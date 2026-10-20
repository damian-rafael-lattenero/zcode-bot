"""workers — los turnos headless del bot (F4g).

La cola (bot-queue.json), el spawn de workers como PROCESOS detached
(F16), prompt_worker (el corazón: sesión → runtime → live view →
entrega), el handoff de huérfanos y bot_stop. El prompt_worker sigue
siendo PROCESO separado (aislamiento real ante /nuclear); el spawn es
subprocess directo. Transporte POR ATRIBUTO (uhttp.*).
"""
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict

from zcode_bot import config
from zcode_bot import proto as zc_proto
from zcode_bot import avisos
from zcode_bot import cli_logs as zc_logs
from zcode_bot import audit
from zcode_bot.core import (BotSession, active_bot_session,
                            set_active_bot_session,
                            _store, _workers, _questions,
                            _stop_request, _stop_requested, _stop_consume,
                            _task_current_ok,
                            _perm_key, _perm_replay, _perm_remember,
                            _model_snapshot, _restore_model, _proto_ready,
                            _create_session, _save_last_prompt,
                            _wait_cli_event,
                            transcript_mb, _timeout_compact,
                            running_turns, _queue_peek, _queue_sids_with_items)
from zcode_bot.live_view import (LiveTurnView, _turn_end_poll, _headless_text,
                                 _rss_mb, _rss_now_mb)
from zcode_bot import razonamiento as cotrz
from zcode_bot.messages import (_fetch_messages, _extract_assistant_text,
                                _last_id_of)
from zcode_bot.reporting import _turn_trace, _send_long_reply
from zcode_bot.nuclear import _nuclear_active
from zcode_bot.models import MODEL_SEL
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log, pwlog


@dataclass
class QueueItem:
    """Un prompt encolado (bot-queue.json). El archivo persiste el shape
    histórico {"act", "text"} + ts (F30, estampado en _queue_put); en
    memoria es tipada con compat mapping — item["text"] e item.get("act")
    siguen andando. Items legacy sin ts: el auditor no los alerta
    (sin dato honesto, sin aviso)."""
    act: Dict[str, Any] = None
    text: str = ""
    ts: float = 0.0

    def to_dict(self):
        return {"act": self.act or {}, "text": self.text, "ts": self.ts}

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(act=d.get("act") if isinstance(d.get("act"), dict) else {},
                   text=str(d.get("text") or ""),
                   ts=float(d.get("ts") or 0.0))

    def keys(self):
        return ("act", "text", "ts")

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def get(self, k, default=None):
        return getattr(self, k, default)


@dataclass
class WorkerEntry:
    """Registro de un worker vivo (state/bot-workers.json, flock). Lo
    construye prompt_worker y lo persiste con to_dict(); los lectores
    (status, nuclear, stop) siguen con .get por compat mapping."""
    t0: float = 0.0
    prompt: str = ""
    ws: str = ""
    titulo: str = ""
    pid: int = 0

    def to_dict(self):
        return {"t0": self.t0, "prompt": self.prompt, "ws": self.ws,
                "titulo": self.titulo, "pid": self.pid}

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(t0=float(d.get("t0") or 0.0),
                   prompt=str(d.get("prompt") or ""),
                   ws=str(d.get("ws") or ""),
                   titulo=str(d.get("titulo") or ""),
                   pid=int(d.get("pid") or 0))

    def get(self, k, default=None):
        return getattr(self, k, default)


def _proc_vivo(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# F39 — /stop honesto: fase del worker y verificación propia del kill
_STOP_ARRANQUE_MAX_S = 600  # worker arrancando hace más de esto = colgado: se mata igual
_STOP_VERIFICA_S = 2.0      # cuánto esperar la muerte tras el kill -9 para confirmarla
_STOP_VERIFICA_PASO_S = 0.2
_PS_TIMEOUT_S = 5


def _proc_muerto(pid):
    """Veredicto del /stop tras el kill. Tras SIGKILL el worker queda
    ZOMBIE hasta que el poller (su padre) lo reapa en el supervisor — y
    os.kill(pid, 0) da por vivo a un zombie: para el usuario un zombie
    ya es un muerto. pid inexistente o ps stat 'Z' → muerto."""
    if not _proc_vivo(pid):
        return True
    try:
        out = (subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                              capture_output=True, text=True,
                              timeout=_PS_TIMEOUT_S).stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return (not out) or out.startswith("Z")


def _cmdline_tiene(pid, flag):
    """¿El pid corre nuestro shim (zcode-telegram.py) con este flag? Un pid
    reciclado puede ser cualquier proceso — incluso un runtime del host,
    que jamás se toca."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True,
                             timeout=_PS_TIMEOUT_S).stdout or ""
        return "zcode-telegram.py" in out and flag in out
    except (OSError, subprocess.SubprocessError):
        return False


def _run_turn_ok(pid):
    """Seguridad ANTES de un killpg: verificar que el pid es un worker
    NUESTRO (zcode-telegram.py --run-turn)."""
    return _cmdline_tiene(pid, "--run-turn")


def _cot_vivo_ok(pid):
    """F44: ídem para un vigilante CoT caliente (su pid-file cot-vivo-*.pid
    puede quedar huérfano tras un kill -9 y apuntar a un pid reciclado)."""
    return _cmdline_tiene(pid, "--cot-vivo")


def _kill_runtime(pid):
    """SIGKILL al GRUPO del worker. El spawn usa start_new_session=True
    → el grupo es worker + app-server hijo: el runtime entero de una."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except OSError:
        return False


def bot_stop(sid, titulo=None, grace=4.0):
    """Frena una corrida iniciada DESDE el celular Y limpia su cola
    (pedido F17: /stop también descarta los mensajes encolados de esa
    task). F16: el frenado va por marker — el WORKER lo ve en su loop y
    ejecuta session/stop con su propio runtime (stop real).
    F7++: session/stop es COOPERATIVO — el turno puede seguir corriendo
    varios segundos (tool en vuelo, generación a mitad). Si el worker no
    muere en `grace` s, escalamos a SIGKILL del grupo completo. El
    cadáver lo levanta el chequeo de huérfanos y se reporta solo."""
    n = len(_queue_take(sid))
    tit = _workers().flag_stop(sid)
    if tit is None:
        if n:
            return ("🧹 Limpié la cola de «%s» (%d mensajes) — no había "
                    "corrida del bot que frenar."
                    % (esc(titulo or sid[-8:]), n))
        return ("🤔 Esa corrida ya no existe (terminó o no fue iniciada "
                "desde el celular).")
    _stop_request(sid)
    # F47c: el turno en curso queda marcado como frenado A PEDIDO — si el
    # worker muere rematado (gracia vencida → kill -9, sin finally), el
    # supervisor de huérfanos lo cuenta como /stop y no como fallo
    _active_turn_mark(sid, stop=time.time())
    extra = (" · 🧹 cola limpiada (%d)" % n) if n else ""
    ent = _workers().get(sid)
    try:
        pid = int((ent or {}).get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 1 and _run_turn_ok(pid):
        # F39: ¿en qué fase está el worker? bot-active.json se escribe al
        # ARRANCAR EL TURNO (_active_turn_register); antes de eso el worker
        # está arrancando el runtime (resume: ~2 min en sesiones grandes)
        # y NO mira el marker hasta terminar — matarlo ahí era el
        # '💀 no frenó en 4s' del 20/10, encima sin reporte posible (el
        # chequeo de huérfanos solo ve lo registrado en bot-active).
        activos = _store("bot-active.json").read()
        en_turno = isinstance(activos, dict) and sid in activos
        try:
            t0w = float((ent or {}).get("t0") or 0) or time.time()
        except (TypeError, ValueError):
            t0w = time.time()
        if not en_turno and time.time() - t0w < _STOP_ARRANQUE_MAX_S:
            # se frena solo al terminar de arrancar, sin correr el prompt
            # ('⏹️ frenado por /stop (antes de enviar)', prompt_worker)
            extra += (" · ⏳ está arrancando el runtime (resume de la "
                      "sesión, puede tardar ~2 min): se frena sola al "
                      "terminar de arrancar, sin correr tu prompt — te lo "
                      "confirma el worker")
            return "🛑 Frenando «%s»…%s" % (esc(tit or titulo or sid[-8:]),
                                           extra)
        t0 = time.time()
        while time.time() - t0 < grace and _proc_vivo(pid) \
                and _workers().get(sid) is not None:
            time.sleep(0.4)
        if _workers().get(sid) is not None and _proc_vivo(pid):
            if _kill_runtime(pid):
                # F39: verificar la muerte ACÁ, sin prometer reportes ajenos
                t1 = time.time()
                while (time.time() - t1 < _STOP_VERIFICA_S
                       and not _proc_muerto(pid)):
                    time.sleep(_STOP_VERIFICA_PASO_S)
                veredicto = ("✅ verificado: proceso muerto, nada corriendo"
                             if _proc_muerto(pid) else
                             "⚠️ el proceso sigue vivo — mirá /status en "
                             "un momento")
                extra += (" · 💀 no respondió al stop en %.0fs: kill -9 del "
                          "runtime · %s" % (grace, veredicto))
                if en_turno:
                    extra += (" · el cierre del turno te llega aparte "
                              "(chequeo de huérfanos)")
                # el worker no va a pasar por su finally: consumir el
                # marker acá o el PRÓXIMO turno de la sesión abortaría solo
                _stop_consume(sid)
    return "🛑 Frenando «%s»…%s" % (esc(tit or titulo or sid[-8:]), extra)


def _q_gemela(sid_eff, huella):
    """rid de la pregunta IDÉNTICA ya pendiente en el canal (mismo sid y
    mismas questions → misma huella): el re-disparo del CLI la reusa sin
    re-mostrar los botones. None si no hay gemela."""
    for q in _questions().pendings():
        if q.get("sid") == sid_eff and _perm_key(
                sid_eff, "AskUserQuestion",
                {"questions": q.get("questions") or []}) == huella:
            return q.get("request_id")
    return None


def _pregunta_msg(qs, titulo):
    """R10 (puro) — (texto, teclado) de la ❓ al chat: una sola pregunta
    mapea botones 1:1 con sus opciones; un cuestionario lista todo sin
    botones (la respuesta va por número). Testeable sin mock."""
    lines = ["❓ <b>%s</b>" % esc(titulo)]
    kb = []
    if len(qs) == 1:
        # una sola pregunta: los botones mapean 1:1 con las opciones
        # que usa la resolución por número (handle_free_text usa qs[0])
        for i, op in enumerate(qs[0].get("options") or [], 1):
            d = (" — " + esc(op["description"][:60])) if op.get("description") else ""
            lines.append("  %d. %s%s" % (i, esc(op.get("label", "?")), d))
            kb.append([{"text": ("%d. %s" % (i, (op.get("label") or "?")))[:36],
                        "callback_data": "ans:%d" % i}])
    else:
        for q in qs:
            if q.get("question"):
                lines.append(esc(q["question"]))
            for i, op in enumerate(q.get("options") or [], 1):
                d = (" — " + esc(op["description"][:60])) if op.get("description") else ""
                lines.append("  %d. %s%s" % (i, esc(op.get("label", "?")), d))
    lines.append("\nRespondé con el número (o tocá un botón)." if kb
                 else "\nRespondé con el número (o texto libre).")
    return "\n".join(lines), ({"inline_keyboard": kb} if kb else None)


def _user_input_handler(cfg, sid, titulo):
    """Callback de interaction/requestUserInput: manda la ❓ al chat y
    espera la respuesta por ARCHIVO (F16: el poller la escribe al tocar
    el botón/número — no hay memoria compartida entre procesos). F15: la
    pregunta lleva SU sid — con varias tasks la respuesta enruta a la que
    preguntó (handle_free_text prioriza la task current)."""
    def handler(params):
        qs = params.get("questions") or []
        sid_eff = sid or (active_bot_session().get("sid"))
        # FIX 14/10 v2 (cuestionarios en loop): el CLI RE-DISPARA la misma
        # AskUserQuestion y el handler re-creaba la entrada y re-mostraba
        # los botones — Damian elegía la opción y volvía a ver la pregunta.
        # (El replay F27 solo cubría el hook del poller, no este camino.)
        # Capas: (1) respondida hace <10 min → devolver directo; (2)
        # idéntica pendiente → reusar su rid SIN re-mostrar; (3) nueva →
        # mostrar.
        huella = _perm_key(sid_eff, "AskUserQuestion", {"questions": qs})
        previa = _perm_replay(huella)
        if previa is not None:
            return {"action": "accept", "content": {"answer": previa}}
        rid = _q_gemela(sid_eff, huella)
        if rid is None:
            rid = str(params.get("requestId") or time.time())
            # /task new: el sid real nace después del spawn — para esa
            # ventana vale el que fija el worker como sesión activa
            _questions().ask(rid, qs, titulo, sid_eff)
            texto, markup = _pregunta_msg(qs, titulo)
            uhttp.send(cfg, texto, loud=True, markup=markup)
        resp = _questions().wait(rid, timeout=480)
        if resp is not None:
            _perm_remember(huella, resp)  # el re-disparo no vuelve a preguntar
            # F36: "✅ Respondido" (del poller) dice que llegó al BOT; la
            # entrega real al agente es ESTA línea — ack del componente
            avisos.eleccion_recibida(
                cfg, "respuesta al agente: %s" % preview(str(resp), 60),
                "el turno sigue con tu elección")
            return {"action": "accept",
                    "content": {"answer": resp}}
        uhttp.send(cfg, "⌛️ La pregunta de «%s» caducó sin respuesta." % esc(titulo))
        return {"action": "decline", "reason": "timeout telegram"}
    return handler


def _queue_put(sid, act, text):
    if isinstance(act, BotSession):
        act = act.to_dict()  # la cola persiste dicts (QueueItem en F4g)
    def add(q):
        q = q if isinstance(q, dict) else {}
        lst = q.get(sid) or []
        lst.append(QueueItem(act, text[:6000], time.time()).to_dict())
        q[sid] = lst[-5:]
        return q
    try:
        # F16: flock — poller y workers-proceso escriben la misma cola
        _store(config.ARCH_COLA).update_cross_process(add, {})
    except OSError:
        pass


def _queue_take(sid):
    taken = [[]]

    def take(q):
        q = q if isinstance(q, dict) else {}
        taken[0] = q.pop(sid, [])
        return q
    try:
        _store(config.ARCH_COLA).update_cross_process(take, {})
    except OSError:
        pass
    return [QueueItem.from_dict(e) for e in taken[0]]


def _queue_drain(cfg, sid):
    """Al terminar un turno, si quedaron prompts encolados para la sesión,
    lanza el siguiente (cadena). F17: si la sesión SIGUE ocupada (corrida
    de la Mac), la cola queda DORMIDA — sin este guard el relay
    re-spawneaba en loop mientras la task estaba ocupada (incidente
    18:03: 5 workers en 5 s, cada uno con su runtime). F15: si dejó de
    ser la task current, también duerme (la retoma /task)."""
    if not sid:
        return
    if _nuclear_active():
        pwlog("drain skip: nuclear en curso (cola dormida: %d)"
              % len(_queue_peek(sid)))
        return
    if sid in running_turns():
        pwlog("drain skip: %s sigue ocupada (cola dormida: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    if not _task_current_ok(cfg, sid):
        pwlog("drain skip: %s no es la task current (cola dormida: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    _queue_launch_next(cfg, sid)


def _requeue(sid, items):
    """R0c — devolver ítems a la cola PRESERVANDO el orden FIFO
    (_queue_put hace append). El bug latente: _warm_wait re-encolaba con
    reversed() + append, que INVERTÍA la cola cuando quedaban ≥2 ítems
    (los mensajes del usuario habrían corrido al revés);
    _queue_launch_next ya lo hacía forward — un solo cuerpo para los tres
    sitios que devuelven ítems."""
    for it in items:
        _queue_put(sid, it["act"], it["text"])


def _subscribe_seq(c, sid):
    """F9 — subscribe + cursor del stream (eventSeq) para esperar el fin
    del turno POR EVENTOS. Si falla, None: _turn_end_poll degrada a
    escanear los logs del CLI (el camino de siempre). R3: las tres copias
    del ritual (turno, resumen de rotación, semilla de sesión nueva)
    viven acá."""
    try:
        r = c.request("session/subscribe",
                      {"sessionId": sid,
                       "deliveryKind": "desktop-continuous"}, timeout=15)
        return ((r or {}).get("result") or {}).get("eventSeq")
    except Exception:  # noqa: BLE001
        return None


# R3: el teclado de reintento de los caminos de error — era TRES literales
_TECLADO_REINTENTA = {"inline_keyboard": [[
    {"text": "🔁 Reintentar", "callback_data": "retry"}]]}


def _sel_actual(snap_sel):
    """F7++ (puro) — la modelSelection del snapshot del turno, que viaja
    como JSON string por el store (lección del 15/10: comparar string
    contra dict daba False SIEMPRE) o como dict. None si no se puede
    leer: sin skip, se restaura."""
    if isinstance(snap_sel, str):
        try:
            return json.loads(snap_sel).get("modelSelection")
        except ValueError:
            return None
    if isinstance(snap_sel, dict):
        return snap_sel.get("modelSelection", snap_sel)
    return None


def _queue_launch_next(cfg, sid):
    """Toma el próximo prompt encolado de la sesión y lanza su worker
    (F16: proceso detached — la cadena de cola sobrevive restarts del
    poller). Devuelve (item, quedan) o None si no había nada."""
    lst = _queue_take(sid)
    if not lst:
        return None
    item = lst[0]
    _requeue(sid, lst[1:])  # los devolvemos a la cola (orden FIFO)
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        _queue_put(sid, item["act"], item["text"])  # no perderlo
        return None
    return item, len(lst) - 1


def _queue_resume(cfg, sid, titulo=None):
    """F15 — auto-resume: al volver una task (con /task) que tenía prompts
    encolados, arranca el primero. Si ya hay un worker vivo de esa sesión,
    la cola sigue sola al terminar su turno."""
    if not sid:
        return None
    if _nuclear_active():
        pwlog("resume skip: nuclear en curso")
        return None
    if _workers().get(sid) is not None:
        n = len(_queue_peek(sid))
        if n:
            return ("⏳ Ya hay un turno corriendo en «%s» — tus %d encolados "
                    "corren al terminar." % (esc(titulo or sid[-8:]), n))
        return None
    res = _queue_launch_next(cfg, sid)
    if not res:
        return None
    quedan = res[1]
    return ("⏳ Retomé la cola de «%s»%s — trabajando…"
            % (esc(titulo or sid[-8:]),
               " (%d más encolados)" % quedan if quedan else ""))


_drain_ts = [0.0]  # throttle propio: el poller llama por ciclo, el
                   # chequeo real corre como mucho cada `cada` segundos


def cola_check(cfg, cada=20.0):
    """F37a — drain autónomo: despierta colas dormidas cuando la sesión
    ya está libre, SIN depender de que otro la avise. Cubre el hueco del
    incidente 19/10 15:47: la cola F17 esperaba el on_stop de un turno
    de la Mac que la app NUNCA lanzó (prompt tragado con la task
    ocupada) — el mensaje durmió 88 minutos hasta el /task manual.
    Mismos guards que _queue_drain: nuclear, worker vivo, sesión ocupada
    y task current (F15: las demás duermen hasta su /task)."""
    ahora = time.time()
    if ahora - _drain_ts[0] < cada:
        return
    _drain_ts[0] = ahora
    if _nuclear_active():
        return
    for sid in _queue_sids_with_items():
        if _workers().get(sid) is not None or sid in running_turns() \
                or not _task_current_ok(cfg, sid):
            continue
        res = _queue_resume(cfg, sid)
        if res:
            uhttp.send(cfg, res, loud=False)


# -------------------------------------------- F8a: worker caliente (ADR-014)

def _queue_rekey(old, new):
    """F8b: mover los prompts encolados de una sesión a otra (rotación).
    Espejo del rekey del registry; atómico por flock."""
    if not old or not new or old == new:
        return

    def fn(q):
        q = dict(q) if isinstance(q, dict) else {}
        lst = q.pop(old, [])
        if not lst:
            return None  # nada que mover: no escribir
        q[new] = (q.get(new) or []) + lst
        return q
    try:
        _store(config.ARCH_COLA).update_cross_process(fn, {})
    except OSError:
        pass


def _warm_wait(cfg, sid, timeout_s):
    """El worker caliente espera el próximo prompt de SU sesión con la
    conexión ya resumida (el resume de ~30s se pagó una vez: mensajes
    seguidos arrancan al instante). Devuelve (text, act) del ítem tomado
    o None (timeout / marker de /stop / la Mac se apropió la sesión).
    Vuelta de ~1s leyendo archivos chicos — sin runtime, sin CPU."""
    t0 = time.time()
    t_hb = 0.0
    while time.time() - t0 < timeout_s:
        if _stop_requested(sid):
            _stop_consume(sid)
            pwlog("warm: marker de /stop — saliendo caliente")
            return None
        if time.time() - t_hb >= 60:
            try:
                _workers().heartbeat(sid)
            except Exception:  # noqa: BLE001
                pass
            t_hb = time.time()
        if _queue_peek(sid):
            lst = _queue_take(sid)
            if lst:
                if sid in running_turns():
                    # F17: la Mac tomó la sesión mientras esperábamos —
                    # devolver TODO y salir (la retoma el camino frío)
                    _requeue(sid, lst)
                    pwlog("warm: task ocupada — re-encolo (%d) y salgo"
                          % len(lst))
                    return None
                _requeue(sid, lst[1:])
                pwlog("warm: pickup (quedan %d en cola)" % (len(lst) - 1))
                return lst[0]["text"], lst[0]["act"]
        time.sleep(1.0)
    pwlog("warm: keepalive vencido (%.0fs) — runtime liberado" % timeout_s)
    return None


# -------------------------------------------- F8b: rotación de sesión

_PROMPT_RESUMEN = (
    "Voy a migrar esta conversación a una sesión nueva por tamaño. "
    "Escribí el resumen de continuidad: objetivo en curso, decisiones "
    "tomadas, estado del código, tareas abiertas y próximos pasos. "
    "Solo el resumen, sin preámbulo ni despedidas.")


def _rotacion_aplazada(cfg, sid, titulo, fase):
    """F28 — llegó un mensaje a mitad de la danza: abortar LIMPIO. Los
    chequeos viven SOLO entre fases y antes de tocar nada persistente
    (bot-active, registry y cola siguen en la sesión vieja) → continuidad
    intacta; el caller cae en _warm_wait, atiende el mensaje en la sesión
    actual y la rotación la re-toma el próximo warm keepalive (la
    condición del gate — cola vacía + transcripto sobre umbral — vuelve
    a dar True)."""
    pwlog("rotación aplazada tras %s (sid %s)" % (fase, sid[-8:]))
    uhttp.send(cfg, "♻️ «%s» — llegó un mensaje tuyo a mitad de la "
               "rotación: la dejo acá (la sesión actual sigue perfecta), "
               "lo atiendo y retomo la rotación cuando quede libre."
               % esc(titulo))
    return None


def _transcript_mb(sid):
    """MB del transcripto (rollout model-io jsonl) — la implementación
    vive en core (la comparte _proto_ready para los timeouts)."""
    return transcript_mb(sid)


def _rotar_sesion(c, cfg, act, sid, ws, titulo, modelo, snap_vieja):
    """F8b: la danza — compact + turno de resumen + sesión nueva sembrada
    con ese resumen (el patrón oficial para resumes pesados: sesiones
    cortas + memoria sembrada). Devuelve (c_nueva, sid_nuevo, act_nuevo)
    o None si algo falló: avisa y SIGUE en la vieja — la continuidad
    jamás se pierde (la sesión vieja queda archivada, accesible /task).
    Interrumpible (F28): mensaje en la cola entre fases → aborta limpio
    vía _rotacion_aplazada, el warm atiende el mensaje en la vieja."""
    mb = _transcript_mb(sid)
    pwlog("rotación: transcripto %.0fMB > umbral — compactando" % mb)
    uhttp.send(cfg, "♻️ «%s» acumuló %.0fMB de transcripto — rotando a "
               "sesión fresca (compact + resumen)…" % (esc(titulo), mb))
    if _stop_requested(sid):
        return None
    try:
        r = c.request("session/compact", {"sessionId": sid},
                      timeout=_timeout_compact(sid))
    except Exception as e:  # noqa: BLE001
        uhttp.send(cfg, "🤔 No pude compactar (%s) — sigo en la sesión "
                   "actual." % esc(str(e)[:100]))
        return None
    if e := zc_proto.proto_err(r, "compact"):
        uhttp.send(cfg, "🤔 El compact fue rechazado — sigo en la sesión "
                   "actual: %s" % esc(e[:120]))
        return None
    fin = _wait_cli_event(sid, time.time(),
                          ("compact.completed", "compact.failed"),
                          _timeout_compact(sid) + 30)
    if fin is None or fin.get("event") == "compact.failed":
        uhttp.send(cfg, "🤔 El compact no terminó bien — sigo en la sesión "
                   "actual.")
        return None
    if _stop_requested(sid):
        return None
    if _queue_peek(sid):
        return _rotacion_aplazada(cfg, sid, titulo, "el compact")
    # turno de resumen: la sesión ya compactada tiene contexto chico
    pre = _fetch_messages(c, sid, limit=1)
    last_id = _last_id_of(pre)
    # F9: cursor del stream para el fin por eventos (degrada a logs si falla)
    seq_res = _subscribe_seq(c, sid)
    try:
        c.request("session/send",
                  {"sessionId": sid,
                   "content": _headless_text(_PROMPT_RESUMEN, cfg),
                   "modelSelection": modelo or MODEL_SEL}, timeout=120)
    except Exception as e:  # noqa: BLE001
        uhttp.send(cfg, "🤔 El turno de resumen no pudo enviarse (%s) — "
                   "sigo en la sesión actual." % esc(str(e)[:100]))
        return None
    _turn_end_poll(sid, time.time(), timeout=300,
                   stop_check=lambda: _stop_requested(sid),
                   t_send=time.time(), conn=c, start_seq=seq_res)
    msgs = _fetch_messages(c, sid, after_id=last_id, limit=5) or []
    resumen = _extract_assistant_text(msgs, solo_ultima=True).strip()[:4000]
    if not resumen:
        uhttp.send(cfg, "🤔 No pude obtener el resumen — sigo en la sesión "
                   "actual.")
        return None
    if _stop_requested(sid):
        return None
    if _queue_peek(sid):
        return _rotacion_aplazada(cfg, sid, titulo, "el resumen")
    # sesión nueva en runtime propio (el resume gigante de la vieja no se
    # vuelve a pagar nunca: el transcripto nuevo arranca en ~KB)
    holder = {"sid": None}

    def on_input(params):
        _user_input_handler(cfg, holder["sid"], titulo)(params)

    def on_perm(params):
        pwlog("🔐 permiso en headless: %s"
              % json.dumps(params, ensure_ascii=False)[:200])
        uhttp.send(cfg, "🔐 «%s» pidió un permiso headless (debería correr "
                   "yolo — avisame para revisarlo): <code>%s</code>"
                   % (esc(titulo), esc(json.dumps(
                       params, ensure_ascii=False)[:120])))
    try:
        c2 = zc_proto.make(cwd=ws, on_user_input=on_input,
                           on_permission=on_perm)
    except OSError as e:
        uhttp.send(cfg, "🤔 No pude lanzar el runtime nuevo (%s) — sigo en "
                   "la sesión actual." % esc(str(e)[:100]))
        return None
    sid2, err = _create_session(c2, ws)
    if err:
        try:
            c2.close()
        except Exception:  # noqa: BLE001
            pass
        uhttp.send(cfg, "🤔 No pude crear la sesión nueva — sigo en la "
                   "actual: %s" % esc(err[:120]))
        return None
    holder["sid"] = sid2
    seed = ("Sos la continuación de una sesión previa de este proyecto "
            "(«%s»), rotada por tamaño. Estado heredado:\n\n%s\n\n"
            "Retomá desde ahí en el próximo mensaje; respondé a este solo "
            "con un OK breve." % (titulo, resumen))
    r = c2.request("session/send",
                   {"sessionId": sid2,
                    "content": _headless_text(seed, cfg),
                    "modelSelection": modelo or MODEL_SEL}, timeout=120)
    if e := zc_proto.proto_err(r, "send"):
        try:
            c2.close()
        except Exception:  # noqa: BLE001
            pass
        uhttp.send(cfg, "🤔 La sesión nueva no aceptó la semilla — sigo en "
                   "la actual: %s" % esc(e[:120]))
        return None
    seq_seed = _subscribe_seq(c2, sid2)
    _turn_end_poll(sid2, time.time(), timeout=180,
                   stop_check=lambda: _stop_requested(sid),
                   t_send=time.time(), conn=c2, start_seq=seq_seed)
    act2 = dict(act, sid=sid2)
    try:
        set_active_bot_session({**active_bot_session(), **act2})
    except Exception:  # noqa: BLE001
        pass
    try:
        _workers().rekey(sid, sid2)  # el botón de /stop apunta a la nueva
    except Exception:  # noqa: BLE001
        pass
    _queue_rekey(sid, sid2)
    _restore_model(sid, snap_vieja)  # la vieja queda limpia (archivada)
    uhttp.send(cfg, "♻️ «%s» rotada: %.0fMB de transcripto → resumen "
               "sembrado. La charla sigue en la sesión nueva <code>%s</"
               "code> (la vieja quedó archivada) — quedo esperando tu "
               "próximo mensaje, escribime nomás."
               % (esc(titulo), mb, esc(sid2[-8:])))
    pwlog("rotación OK: %s -> %s (resumen %d chars)"
          % (sid, sid2, len(resumen)))
    return c2, sid2, act2


def _active_turn_register(sid, titulo, t0, ws=None, last_id=None):
    """Registro persistente del turno en curso: si el worker muere SIN
    correr su finally (kill -9 / OOM), el supervisor del poller lo ve y
    entrega el final — nadie muere en silencio."""
    if not sid:
        return

    def add(q):
        q = q if isinstance(q, dict) else {}
        q[sid] = {"titulo": titulo, "t0": t0, "pid": os.getpid(),
                  "ws": ws, "last_id": last_id}
        return q
    try:
        _store("bot-active.json").update_cross_process(add, {})
    except OSError:
        pass


def _active_turn_clear(sid):
    if not sid:
        return

    def rm(q):
        q = q if isinstance(q, dict) else {}
        q.pop(sid, None)
        return q
    try:
        _store("bot-active.json").update_cross_process(rm, {})
    except OSError:
        pass


def _active_turn_mark(sid, **campos):
    """Anota campos en el registro del turno en curso SI existe (no lo
    crea): /stop deja stop=<epoch> para que el supervisor de huérfanos, si
    el worker muere rematado, redacte «frenada» y no «falló» (F47c)."""
    if not sid:
        return

    def upd(q):
        q = q if isinstance(q, dict) else {}
        ent = q.get(sid)
        if isinstance(ent, dict):
            ent.update(campos)
        return q
    try:
        _store("bot-active.json").update_cross_process(upd, {})
    except OSError:
        pass


def _spawn_worker(cfg, act, text):
    """F16: lanza el worker como PROCESO detached (start_new_session=True,
    patrón del stuck-watch). Un kickstart/restart/crash del poller NO lo
    toca: el turno vive su ciclo completo y se reporta solo por Telegram.
    Payload JSON por stdin (argv se queda corto para prompts de 6KB)."""
    if _nuclear_active():
        # Nuclear 2.0 — cerradura: NADA nuevo arranca durante el ciclo
        # (antes el poller podía spawnear un worker a mitad del nuclear
        # que moría con el kickstart sin reporte). La cola queda dormida
        # y sobrevive: el auto-resume la drena al volver.
        pwlog("spawn bloqueado: nuclear en curso")
        return None
    _save_last_prompt(act, text)  # F22: /retry siempre tiene qué relanzar
    if isinstance(act, BotSession):
        act = act.to_dict()  # el mailbox por stdin viaja como dict plano
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")  # shim: entrypoint de subprocesos
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        with open(os.path.join(config.LOG_DIR, "worker-stderr.log"), "a") as err:
            p = subprocess.Popen(
                [sys.executable, me, "--run-turn"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                start_new_session=True,
                cwd=act.get("ws") or os.path.expanduser("~"))
            p.stdin.write(json.dumps({"act": act, "text": text}).encode("utf-8"))
            p.stdin.close()
        _SPAWNED.append(p)  # F30: el orphans_check lo reapa al morir
        return p
    except OSError as e:
        audit.registrar((act or {}).get("sid"), "spawn",
                        "no pude lanzar el worker: %s" % str(e)[:120])
        uhttp.send(cfg, "❌ No pude lanzar el worker: " + esc(str(e)[:120]))
        return None


# F30: Popens de workers de ESTE proceso. El poller nunca hacía wait() →
# cada worker muerto quedaba <defunct> para siempre (13066 del incidente
# 18:10). El orphans_check llama _reapar_hijos cada ciclo: Popen.poll es
# waitpid WNOHANG — reclama los muertos, conserva los vivos, cero bloqueo.
_SPAWNED = []


def _reapar_hijos():
    vivos = []
    for p in _SPAWNED:
        try:
            if p.poll() is None:
                vivos.append(p)
        except Exception:  # noqa: BLE001 — el reap jamás rompe el supervisor
            pass
    _SPAWNED[:] = vivos


def cmd_run_turn():
    """F16: entrypoint del worker-proceso (--run-turn). Lee {act, text} de
    stdin, instala el SIGTERM → marker (nuclear amable: el shutdown pasa
    por el mismo camino que /stop) y un atexit de seguridad para el
    registry; luego corre el mismo prompt_worker de siempre."""
    import atexit
    import signal
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    act = payload.get("act") or {}
    text = payload.get("text") or ""
    sid = act.get("sid")

    def _sig(signum, frame):
        _stop_request(sid)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (OSError, ValueError):
        pass
    atexit.register(lambda: _workers().pop_if_mine(sid or "nueva"))
    prompt_worker(load_config(), act, text)


def _correr_turno(c, cfg, act, text, sid, ws, titulo, modelo, stop_hook,
                  ph=None):
    """F8a: UN turno completo sobre una sesión YA activa (fría o caliente —
    misma conexión, mismo protocolo; la diferencia es solo si ph nace acá
    o viene del arranque en frío). Entrega por Telegram y devuelve:
    'ok' | 'error' (el warm puede seguir) | 'stop'. R0b: ya NO existe el
    'abandonado' — mientras el turno viva se espera (aviso cada 30 min);
    el único corte es /stop. El ciclo de vida del worker
    (registry/bot-active/marker/runtime/cola) queda afuera, en
    prompt_worker."""
    import threading as _th
    if ph is None and cfg.get("live_progress"):
        # turno CALIENTE: la caja llega igual, pero instantánea
        ph = uhttp._live_send(cfg, "🧠 <b>«%s»</b> recibido — corriendo…"
                              % esc(titulo))
    try:
        if _stop_requested(sid):
            # /stop llegó antes de enviar: ni mandar el turno
            uhttp.send(cfg, "⏹️ «%s» frenado por /stop (antes de enviar)." % esc(titulo))
            if ph:
                uhttp._live_edit(cfg, ph, "⏹️ <b>«%s»</b> frenado por /stop"
                                 % esc(titulo))
            return "stop"
        pre = _fetch_messages(c, sid, limit=1)
        last_id = _last_id_of(pre)
        t0 = time.time()
        # F9: subscribe ANTES del send — el cursor (eventSeq) sale de acá
        # y garantiza que turn.started/completed del turno ENTREN en el
        # stream (con afterSeq incremental, frames en KB). Si falla,
        # start_seq=None y _turn_end_poll degrada a escanear logs.
        start_seq = _subscribe_seq(c, sid)
        r = c.request("session/send",
                      {"sessionId": sid, "content": _headless_text(text, cfg),
                       "modelSelection": modelo or MODEL_SEL}, timeout=120)
        pwlog("send resp %.1fs:" % (time.time() - t0), json.dumps(r)[:200])
        if e := zc_proto.proto_err(r, "send"):
            audit.registrar(sid, "send", e[:150])  # F30: correlación
            uhttp.send(cfg, e)
            if ph:
                uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> falló: %s"
                                 % (esc(titulo), e))
            return "error"
        t_send = time.time()  # F20: ancla del turnId PROPIO (turno cruzado)
        _active_turn_register(sid, titulo, t0, ws=ws, last_id=last_id)

        def aviso_largo(minutos):
            # R0b: mientras el turno VIVA, acá estamos — el abandono a los
            # 40 min mataba el runtime con él ("Turn was cancelled" al
            # mismo segundo que el worker salía, 20/10 14:55).
            pwlog("turno largo: %d min — sigo esperando" % minutos)
            uhttp.send(cfg, "⏱️ «%s» lleva %d min corriendo — sigo con él "
                      "(/stop frena)." % (esc(titulo), minutos), loud=False)

        espera = dict(timeout=None, stop_check=stop_hook, t_send=t_send,
                      conn=c, start_seq=start_seq, aviso=aviso_largo)
        if ph:
            res = {}
            th = _th.Thread(target=lambda: res.update(
                fin=_turn_end_poll(sid, t0, **espera)),
                daemon=True)
            th.start()
            lr = LiveTurnView(c, cfg, sid, t0, titulo, ph, th,
                              last_id=last_id, start_seq=start_seq).run()
            live_cot = (lr[0] if lr else "") or ""
            live_box = (lr[3] if lr else None) or ph  # caja viva (abajo si hubo CoT)
            fin = res.get("fin")
            if fin is None:  # la vista cortó por SU tope: el turno sigue — esperar
                fin = _turn_end_poll(sid, t0, **espera)
        else:
            fin = _turn_end_poll(sid, t0, **espera)
            live_cot = ""
            live_box = ph
        pwlog("fin:", (fin or {}).get("event"), "rss=%dMB rss_now=%dMB"
              % (_rss_mb(), _rss_now_mb()))
        if ph:
            # R0b: fin NUNCA es None acá — la espera no tiene tope
            if fin.get("event") == "turn.failed":
                uhttp._live_edit(cfg, live_box, "❌ <b>«%s»</b> falló — detalle abajo"
                           % esc(titulo))
            else:
                uhttp._live_edit(cfg, live_box, "✅ <b>«%s»</b> · respuesta:" % esc(titulo))
        if fin.get("event") == "turn.failed":
            err = str((fin.get("error") or {}).get("message", ""))[:150]
            audit.registrar(sid, "turn-failed", err)  # F30: correlación
            uhttp.send(cfg, "❌ «%s» falló: %s" % (esc(titulo), esc(err)),
                 markup=_TECLADO_REINTENTA)
            return "error"
        if _workers().stopped(sid):
            if ph:
                uhttp._live_edit(cfg, live_box, "⏹️ <b>«%s»</b> frenado" % esc(titulo))
            uhttp.send(cfg, "⏹️ «%s» frenado por /stop." % esc(titulo))
            return "stop"
        dur = (time.time() - t0) / 60.0
        msgs = _fetch_messages(c, sid, after_id=last_id, limit=120) or []
        # F20: SOLO el último mensaje assistant — en sesión compartida con
        # la app hay respuestas ajenas en la ventana (incidente 18:29)
        texto = _extract_assistant_text(msgs, solo_ultima=True).strip()
        _turn_trace(cfg, titulo, sid, dur, msgs, live_cot=live_cot)
        if not texto:
            uhttp.send(cfg, "✅ «%s» listo en %.0f min (sin texto de respuesta)." % (esc(titulo), dur))
            return "ok"
        _send_long_reply(cfg, titulo, dur, texto)
        return "ok"
    except Exception as e:
        # un worker muerto NUNCA más es silencioso: avisar al chat + log
        import traceback
        pwlog("worker CRASH:", traceback.format_exc()[-500:])
        audit.registrar(sid, "crash", str(e)[:150])  # F30: correlación
        try:
            uhttp.send(cfg, "⚠️ El worker de «%s» murió procesando tu mensaje: "
                  "<code>%s</code>" %
                  (esc(titulo), esc(str(e)[:150])), loud=True,
                  markup=_TECLADO_REINTENTA)
        except Exception:
            pass
        return "error"
    finally:
        # limpieza POR TURNO (el warm puede correr el próximo): la
        # entrada activa y el marker no deben sobrevivir al turno
        _active_turn_clear(sid)
        _stop_consume(sid)


def prompt_worker(cfg, act, text):
    """Corre un prompt headless en la conversación activa y empuja la
    respuesta a Telegram. Vive en un PROCESO (F16: actor). F8a: si
    warm_keepalive_s > 0, tras el turno queda CALIENTE — mismo runtime,
    mismo sid, esperando el próximo prompt de la sesión (_warm_wait) —
    y rota la sesión si el transcripto creció demasiado (F8b)."""
    sid, ws, titulo = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    key = sid or "nueva"
    if sid and _workers().get(sid) is not None:
        # ya hay un worker para la sesión (carrera poller/queue_drain):
        # encolar sin avisar — el que corre hace drain al terminar
        _queue_put(sid, act, text)
        return
    if sid and sid in running_turns():
        # F17: la task está ocupada (corrida de la Mac, sin worker nuestro)
        # → cola DORMIDA, sin spawnnear runtime (el relay en loop gastaba
        # ~500MB por ciclo). La despierta quien termina: on_stop de la Mac
        # o el drain del worker.
        pwlog("busy -> encolar (task ocupada, sin runtime)")
        _queue_put(sid, act, text)
        uhttp.send(cfg, "⏳ La Mac está ocupada con «%s» — tu mensaje quedó en "
                  "la cola (%d en espera). Corre al terminar la corrida "
                  "(o /stop para limpiarla)."
                  % (esc(titulo), len(_queue_peek(sid))))
        return
    _workers().register(key, WorkerEntry(
        t0=time.time(), prompt=text[:80], ws=ws, titulo=titulo,
        pid=os.getpid()).to_dict())
    pwlog("worker start sid=%s pid=%d titulo=%s rss=%dMB texto=%r"
          % (sid, os.getpid(), titulo, _rss_mb(), text[:60]))
    # F7+: el placeholder llega en ~2s (antes de tocar el runtime) — el
    # arranque completo (resume de sesión grande) puede tardar decenas de
    # segundos y el chat no debe quedar mudo esperando
    ph = None
    if cfg.get("live_progress"):
        ph = uhttp._live_send(cfg, "🧠 <b>«%s»</b> arrancando…" % esc(titulo))
    t_arr = time.time()
    try:
        c = zc_proto.make(cwd=ws,
                          on_user_input=_user_input_handler(cfg, sid, titulo),
                          on_permission=lambda params: (
                               pwlog("🔐 permiso en headless: %s"
                                     % json.dumps(params, ensure_ascii=False)[:200]),
                               uhttp.send(cfg, "🔐 «%s» pidió un permiso headless "
                                         "(debería correr yolo — avisame para "
                                         "revisarlo): <code>%s</code>"
                                         % (esc(titulo), esc(json.dumps(
                                             params, ensure_ascii=False)[:120])))))
        if ph:
            # F7+++: el arranque de una sesión grande tarda decenas de
            # segundos — que la caja muestre EN QUÉ está, no "arrancando"
            uhttp._live_edit(cfg, ph,
                             "🧠 <b>«%s»</b> abriendo sesión (historial "
                             "grande: puede tardar un minuto)…" % esc(titulo))
    except OSError as e:
        _workers().pop(key)  # OJO key: pop(None) dejaba el orphan "nueva" (bug F0)
        audit.registrar(sid, "runtime", str(e)[:120])  # F30: correlación
        uhttp.send(cfg, "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120]))
        if ph:
            uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> no pudo lanzar el runtime"
                             % esc(titulo))
        return
    # F16: /stop pasa por marker (state/bot-stop/<sid>) — el ProtoClient
    # ya no se comparte con el poller (los pipes no cruzan procesos)
    snap_sel = None  # /stop puede frenar ANTES de la asignación (finally la lee)
    stop_ya = []

    def stop_hook():
        """F16: el gancho de _turn_end_poll — si el poller dejó marker de
        /stop, frenar con el runtime PROPIO del worker (stop real)."""
        if stop_ya or not _stop_requested(sid):
            return
        stop_ya.append(1)
        pwlog("stop marker: session/stop %s" % sid)
        try:
            c.request("session/stop", {"sessionId": sid}, timeout=30)
            # F36: el stop real lo ejecuta ESTE proceso y su resultado
            # nunca volvía al celu (el poller solo dice "Frenando…" tras
            # su grace de 4s) — la app confirmó → ack del componente
            avisos.eleccion_recibida(cfg, "frenado «%s»" % titulo,
                                     "la app confirmó el frenado")
        except Exception:  # noqa: BLE001
            pass
    try:
        if not sid:  # /task new: crear la sesión AHORA (vive durante el turno)
            sid, err = _create_session(c, ws)
            if err:
                uhttp.send(cfg, err)
                if ph:
                    uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> falló: %s"
                                     % (esc(titulo), err))
                return
            act = dict(act, sid=sid)
            set_active_bot_session(act)
            _workers().rekey("nueva", sid)  # el botón de /stop apunta al sid real
        modelo = act.get("model")
        mode = act.get("mode") or "yolo"
        snap_sel = None if sid in (None, "nueva") else _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot: nada que restaurar
        # F7++: saltar los sets NO-OP — cada request devuelve el historial
        # COMPLETO (26MB ≈ 30s en una sesión grande, x3 por turno). El
        # modo se valida contra el STORE además del act (los acts
        # encolados son snapshots viejas que no traen mode_set).
        sel_actual = _sel_actual(snap_sel)
        skip_model = bool(sel_actual) and sel_actual == (modelo or MODEL_SEL)
        skip_mode = mode in (act.get("mode_set"),
                             active_bot_session().get("mode_set"))
        err = _proto_ready(c, sid, model_sel=modelo, mode=mode,
                           skip_model=skip_model, skip_mode=skip_mode)
        pwlog("proto listo %.1fs (skip_model=%s skip_mode=%s)"
              % (time.time() - t_arr, skip_model, skip_mode))
        if err:
            # incidente 18:10: este camino era SILENCIOSO en el log (solo
            # el mensaje al chat) — una hora de diagnóstico por esa nada
            pwlog("proto falló: %s" % err[:150])
            audit.registrar(sid, "proto", err[:150])  # F30: correlación
            uhttp.send(cfg, err)
            if ph:
                uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> falló: %s"
                                 % (esc(titulo), err))
            return
        if not skip_mode:
            # registrar el modo aplicado: el próximo turno lo salta
            try:
                set_active_bot_session({**active_bot_session(),
                                        **act, "mode_set": mode})
            except Exception:  # noqa: BLE001
                pass
        if ph:
            uhttp._live_edit(cfg, ph,
                             "🧠 <b>«%s»</b> sesión lista — enviando…"
                             % esc(titulo))
        if _stop_requested(sid):
            # /stop llegó durante el arranque: ni mandar el turno
            uhttp.send(cfg, "⏹️ «%s» frenado por /stop (antes de enviar)." % esc(titulo))
            if ph:
                uhttp._live_edit(cfg, ph, "⏹️ <b>«%s»</b> frenado por /stop"
                                 % esc(titulo))
            return
        est = _correr_turno(c, cfg, act, text, sid, ws, titulo, modelo,
                            stop_hook, ph=ph)
        # F8a: ciclo caliente — el runtime NO muere con el turno: espera
        # el próximo prompt de ESTA sesión con la conexión ya resumida
        # (patrón estado-vivo, ADR-014: el resume de ~30s se paga una vez
        # y los mensajes seguidos arrancan al instante).
        # warm_keepalive_s=0 → un proceso por turno, el comportamiento de
        # siempre (y el default de los tests).
        keepalive = int(cfg.get("warm_keepalive_s") or 0)
        rotar_mb = float(cfg.get("rotar_umbral_mb") or 0)
        while keepalive > 0 and est in ("ok", "error"):
            # F8b: rotación SOLO con la cola vacía (jamás bloquea un
            # mensaje del usuario) ni con la Mac en la sesión — y F28:
            # si llega DURANTE la danza, _rotacion_aplazada la difiere
            if (rotar_mb > 0 and not _queue_peek(sid)
                    and sid not in running_turns()
                    and _transcript_mb(sid) > rotar_mb):
                res_rot = _rotar_sesion(c, cfg, act, sid, ws, titulo,
                                        modelo, snap_sel)
                if res_rot is not None:
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass
                    c, sid, act = res_rot
                    modelo = act.get("model")
                    snap_sel = None  # sesión nueva: nada que restaurar
            item = _warm_wait(cfg, sid, keepalive)
            if item is None:
                break
            texto2, act2 = item
            # el ítem trae el act fresco (/task pudo cambiar modelo/modo)
            act, modelo = act2, act2.get("model")
            est = _correr_turno(c, cfg, act, texto2, sid, ws, titulo,
                                modelo, stop_hook, ph=None)
    except Exception as e:
        # un worker muerto NUNCA más es silencioso: avisar al chat + log
        import traceback
        pwlog("worker CRASH:", traceback.format_exc()[-500:])
        audit.registrar(sid, "crash", str(e)[:150])  # F30: correlación
        try:
            uhttp.send(cfg, "⚠️ El worker de «%s» murió procesando tu mensaje: "
                  "<code>%s</code>" %
                  (esc(titulo), esc(str(e)[:150])), loud=True,
                  markup=_TECLADO_REINTENTA)
        except Exception:
            pass
    finally:
        _active_turn_clear(sid)
        _workers().pop_if_mine(sid or "nueva")  # solo la PROPIA (incidente 15:40)
        if sid:
            _stop_consume(sid)  # F16: marker consumido — no frenar el próximo turno
        try:
            c.close()
        except Exception as e:
            log("errors", "worker close: %s" % e)
        if sid and snap_sel:
            try:
                _restore_model(sid, snap_sel)
            except Exception as e:
                log("errors", "worker restore: %s" % e)
        try:
            _queue_drain(cfg, sid)
        except Exception:
            pass


def _runtime_muerto(c):
    """True si el runtime hijo del worker ya no existe (OOM/kill)."""
    p = getattr(c, "p", None)
    try:
        return p is not None and p.poll() is not None
    except OSError:
        return True


def _orphan_fetch_text(ent, sid):
    """F21: texto de la respuesta de un turno huérfano ya completado.
    Runtime efímero: resume del sid y fetch — SIN setModel/setMode para
    no pisar la configuración de la sesión. Nunca lanza."""
    try:
        c = zc_proto.make(cwd=ent.get("ws") or config.DEFAULT_WS)
        try:
            # 120s: rescate best-effort en el supervisor del poller —
            # alcanza para sesiones grandes sin colgar el loop supervisor
            r = c.request("session/resume", {"sessionId": sid}, timeout=120)
            if "error" in r:
                return None
            msgs = _fetch_messages(c, sid, after_id=ent.get("last_id"),
                                   limit=120) or []
            return _extract_assistant_text(msgs, solo_ultima=True).strip()
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None


def _orphans_check(cfg):
    """F21 — supervisor de huérfanos (corre en cada vuelta del poller):
    turnos del bot cuyo worker murió abruptamente. El finally del worker
    siempre limpia su registro → entrada con pid muerto = muerte SIN
    cleanup → aviso honesto en vez de silencio eterno. Barato: un read +
    un os.kill por entrada. F30: además reapa los Popens hijos (zombies)."""
    _reapar_hijos()
    q = _store("bot-active.json").read()
    if not isinstance(q, dict) or not q:
        return
    ahora = time.time()
    for sid, ent in list(q.items()):
        if not isinstance(ent, dict):
            continue
        t0 = float(ent.get("t0") or 0)
        if ahora - t0 > 7200:  # 2h: purge silencioso (anti PID-reuse)
            _active_turn_clear(sid)
            continue
        pid = ent.get("pid")
        try:
            os.kill(int(pid), 0)
            continue  # vivo: worker sano, nada que hacer
        except (OSError, ValueError, TypeError):
            pass
        # pid muerto: ¿llegó a terminar el turno antes de caer el worker?
        fin = None
        for _ts, _s, _ev, d in zc_logs.iter_events(
                config.BASE, needles=("turn.completed", "turn.failed"),
                sids={sid}, since_epoch=t0, last_files=1,
                tail_bytes=zc_logs.LOG_TAIL):
            fin = d
        titulo = ent.get("titulo") or sid[-8:]
        por_stop = bool(ent.get("stop"))  # F47c: lo frenó el usuario
        motivo = None  # con qué se cierra el cache CoT (None = normal)
        if fin is not None and fin.get("event") == "turn.completed":
            # entrega REAL del texto: runtime efímero de solo-lectura
            # (resume del sid SIN setModel — no pisa la configuración)
            texto = _orphan_fetch_text(ent, sid)
            if texto:
                _send_long_reply(
                    cfg, titulo, (ahora - t0) / 60.0, texto)
                pwlog("orphan", sid[-8:], "entregado (%d chars)" % len(texto))
            else:
                uhttp.send(cfg, "🛟 El worker de «%s» murió, pero su turno "
                      "TERMINÓ — la respuesta quedó en la sesión (/cot "
                      "para el trace)." % esc(titulo))
                pwlog("orphan", sid[-8:], "fin=completed (texto no recuperable)")
        elif fin is not None and por_stop:
            # el /stop venció la gracia → kill -9 → sin finally: el turno
            # quedó cancelado porque el usuario lo pidió, no «falló»
            uhttp.send(cfg, "⏹️ «%s» frenada por tu /stop — el turno quedó "
                       "cancelado y el worker se cerró en seco (no respondió "
                       "al stop a tiempo)." % esc(titulo))
            pwlog("orphan", sid[-8:], "fin=failed (por /stop)")
            audit.registrar(sid, "stop", "turno cancelado por /stop, worker "
                            "rematado al vencer la gracia")
            motivo = "frenado por /stop"
        elif fin is not None:
            uhttp.send(cfg, "❌ «%s» falló (el worker había muerto): %s"
                 % (esc(titulo), esc(str((fin.get("error") or {})
                                         .get("message", ""))[:150])))
            pwlog("orphan", sid[-8:], "fin=failed")
            audit.registrar(sid, "turn-failed",
                            "worker muerto, el turno terminó failed")
            motivo = "worker muerto, el turno terminó failed"
        elif por_stop:
            uhttp.send(cfg, "⏹️ «%s» frenada por tu /stop en seco — el runtime "
                       "murió antes de cerrar el turno; no va a llegar "
                       "respuesta." % esc(titulo))
            pwlog("orphan", sid[-8:], "sin fin (por /stop)")
            audit.registrar(sid, "stop", "worker rematado por /stop sin fin "
                            "de turno")
            motivo = "frenado por /stop"
        else:
            uhttp.send(cfg, "⛔️ El turno de «%s» murió con su worker (kill/OOM) — "
                  "no va a llegar respuesta. Reenviá el mensaje cuando "
                  "quieras." % esc(titulo))
            pwlog("orphan", sid[-8:], "sin fin (runtime muerto)")
            audit.registrar(sid, "turno-muerto",
                            "worker murió sin fin de turno (kill/OOM)")
            motivo = "worker murió sin fin de turno (kill/OOM)"
        # F47d: el cache CoT del turno quedó con el último vivo=True del
        # worker — su cierre murió con él; acá nadie más lo cierra
        cotrz.cerrar_cache(sid, t0, **({"error": motivo} if motivo else {}))
        _active_turn_clear(sid)
        # la cola de esa sesión quedó dormida: despertarla si es la current
        try:
            if _task_current_ok(cfg, sid):
                _queue_resume(cfg, sid, titulo)
        except Exception:  # noqa: BLE001
            pass
