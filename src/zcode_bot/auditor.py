"""auditor — observador de invariantes entre componentes (F30).

El sistema inmunitario que faltaba: orphans_check, stuck-watch y outbox
son auto-chequeos INTRA-componente; este módulo mira las FRONTERAS como
caja negra, por archivos, sin locks ni camino caliente. Las cuatro
invariantes nacieron de incidentes reales del 15/10:

1. correlación de fallos  → "sesión enferma" al 2º intento idéntico, no
   al cuarto (incidente 18:10: 4 /compact y 2 turnos con el mismo error).
2. markers de /stop huérfanos → el nuclear SIGTERMat al worker (el
   handler CREA el marker) y el SIGKILL posterior le impide consumirlo:
   el marker frenaba el PRÓXIMO turno de rebote (13066, 18:13).
3. workers fantasma → run-turn vivo sin entrada en registry (registry
   vacío = /stop, /status y el guard F15 ciegos — incidente 15:40).
4. cola dormida → items encolados que nadie va a tomar (el flujo F17
   normal es "Mac ocupada"; lo anómalo es encolado SIN corrida en vuelo).

Filosofía: SANA lo idempotente (consumir marker huérfano), RECOMIENDA lo
dependiente de estado (rotar sesión enferma, /stop go a un fantasma,
/qgo a la cola) — jamás ejecuta acciones destructivas por su cuenta.
Anti-spam: una alerta por clave cada `auditor_alerta_cooldown_min`.
"""
import os
import time

from zcode_bot import audit
from zcode_bot import config
from zcode_bot import nuclear as zc_nuclear
from zcode_bot import workers as zc_workers
from zcode_bot.core import _store, _stop_consume, running_turns
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc

# Ventana de correlación de fallos (10 min: el incidente 18:10/18:12
# cabía entero en 2 min; 10 cubre reintentos espaciados)
VENTANA_FALLOS_S = 600
# Anti-carrera del marker huérfano: un spawn puede estar en vuelo
MARKER_HUERFANO_S = 60
# Cola sin quien la tome (el F17 normal es "Mac ocupada" y NO alerta)
COLA_DORMIDA_MIN = 10

_ULTIMA = [0.0]  # throttle interno: la cadencia la fija el knob, no el loop


def auditar(cfg, ahora=None, pids_run_turn=None, es_run_turn=None):
    """Un pase de invariantes. Corre en el supervisor del poller con
    throttle propio (auditor_cada_s; 0 = apagado). Cada invariante es
    best-effort: una rota, las otras corren — y NINGUNA lanza al
    supervisor (mismo contrato que on_* en hooks)."""
    ahora = time.time() if ahora is None else ahora
    cada = float(cfg.get("auditor_cada_s") or 0)
    if cada <= 0:
        return
    if ahora - _ULTIMA[0] < cada:
        return
    _ULTIMA[0] = ahora
    for chequeo in (_correlacion_fallos, _markers_huerfanos,
                    _workers_fantasma, _cola_dormida):
        try:
            chequeo(cfg, ahora, pids_run_turn=pids_run_turn,
                    es_run_turn=es_run_turn)
        except Exception:  # noqa: BLE001 — el auditor jamás rompe el poller
            pass


def _alertar(cfg, clave, texto, ahora):
    """Anti-spam: una alerta por clave cada `auditor_alerta_cooldown_min`.
    La marca queda en el registro (tipo=alerta, campo clave)."""
    cooldown = float(cfg.get("auditor_alerta_cooldown_min") or 60) * 60
    for ev in audit.leer():
        if ev.get("tipo") == "alerta" and ev.get("clave") == clave \
                and ahora - float(ev.get("ts") or 0) < cooldown:
            return
    audit.registrar(None, "alerta", detalle=texto, extra={"clave": clave})
    uhttp.send(cfg, texto, loud=False)


# ------------------------------------------------------- invariantes

def _correlacion_fallos(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """≥ N fallos del mismo (sid, tipo) en la ventana → "sesión enferma".
    El bucket sid=None agrupa fallos globales (envíos): el incidente
    429/deadlock de las 15:47 habría alertado por acá."""
    minimo = int(cfg.get("auditor_fallos_min") or 2)
    grupos = {}
    for ev in audit.leer():
        if ev.get("tipo") == "alerta":
            continue  # las alertas propias no son fallos a correlacionar
        ts = float(ev.get("ts") or 0)
        if ts < ahora - VENTANA_FALLOS_S:
            continue
        grupos.setdefault((ev.get("sid"), ev.get("tipo")), []).append(ts)
    for (sid, tipo), tss in grupos.items():
        if len(tss) < minimo:
            continue
        mins = max(1, int((ahora - min(tss)) / 60))
        if sid:
            clave = "fallos|%s|%s" % (sid, tipo)
            texto = ("⚠️ sess_%s: %d fallos de «%s» en %d min — sesión "
                     "enferma. Sugerencia: /task a otra conversación o "
                     "esperar la rotación." % (sid[-8:], len(tss), tipo, mins))
        else:
            clave = "fallos|global|%s" % tipo
            texto = ("⚠️ %d fallos de «%s» en %d min en los envíos — revisá "
                     "conectividad o /ping." % (len(tss), tipo, mins))
        _alertar(cfg, clave, texto, ahora)


def _markers_huerfanos(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """Marker de /stop sin dueño → consumir (sanación silenciosa). El
    dueño es el worker del sid: vivo en registry O turno activo. El
    umbral de 60s cubre la carrera con un spawn en vuelo. El filename es
    el sid sanitizado y los sids reales no tienen caracteres que el
    sanitize toque → filename == sid."""
    d = config.stop_dir()
    try:
        nombres = os.listdir(d)
    except OSError:
        return
    activos = running_turns()
    for nombre in nombres:
        ruta = os.path.join(d, nombre)
        try:
            if ahora - os.path.getmtime(ruta) <= MARKER_HUERFANO_S:
                continue  # fresco: un spawn puede estar en vuelo
        except OSError:
            continue
        if nombre in activos:
            continue
        if zc_workers._workers().get(nombre) is not None:
            continue  # worker vivo: dueño legítimo
        _stop_consume(nombre)
        audit.registrar(nombre, "marker-huerfano",
                        "consumido sin dueño (registry vacío, sin turno)")


def _workers_fantasma(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """run-turn VIVO sin entrada en registry → recomendar /stop go. Con
    registry vacío, /stop, /status y el guard F15 del stuck-watch quedan
    ciegos (incidente 15:40) — esto lo hace visible."""
    pids_run_turn = pids_run_turn or (lambda: zc_nuclear._nuc_pids(
        ["-f", "zcode-telegram.py --run-turn"]))
    es_run_turn = es_run_turn or zc_workers._run_turn_ok
    registrados = set()
    for _sid, w in zc_workers._workers().items():
        try:
            registrados.add(int(w.get("pid") or 0))
        except (TypeError, ValueError):
            continue
    fantasmas = [p for p in pids_run_turn()
                 if p != os.getpid() and p not in registrados
                 and es_run_turn(p)]
    if not fantasmas:
        return
    texto = ("🧟 Worker sin registro (pid %s): /stop, /status y el stuck-watch "
             "no lo ven — /stop go para rematarlo o /nuclear si persiste."
             % ", ".join(str(p) for p in fantasmas))
    _alertar(cfg, "fantasma|%s" % ",".join(str(p) for p in fantasmas),
             texto, ahora)


def _cola_dormida(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """Encolado sin worker y sin corrida en vuelo por > 10 min → nadie va
    a tomarlo (el F17 "Mac ocupada" tiene corrida y NO alerta acá). Los
    items legacy sin ts no alertan (sin dato honesto, sin aviso)."""
    q = _store(config.ARCH_COLA).read()
    if not isinstance(q, dict):
        return
    activos = running_turns()
    for sid, items in q.items():
        if not isinstance(items, list) or not items:
            continue
        if zc_workers._workers().get(sid) is not None:
            continue  # worker vivo: la va a tomar al terminar
        if sid in activos:
            continue  # Mac ocupada: F17, comportamiento normal
        tss = [float(i.get("ts") or 0) for i in items
               if isinstance(i, dict) and i.get("ts")]
        if not tss:
            continue  # legacy sin ts
        mins = (ahora - min(tss)) / 60.0
        if mins < COLA_DORMIDA_MIN:
            continue
        titulo = ((items[0].get("act") or {}).get("title") or sid[-8:]) \
            if isinstance(items[0], dict) else sid[-8:]
        texto = ("📬 %d mensaje%s encolado%s para «%s» llevan %d min sin "
                 "corrida que los tome — /qgo para lanzarlos o /task a la "
                 "conversación." % (len(items),
                                    "s" if len(items) != 1 else "",
                                    "s" if len(items) != 1 else "",
                                    esc(str(titulo)), int(mins)))
        _alertar(cfg, "cola|%s" % sid, texto, ahora)
