"""razonamiento — el CoT de los turnos de la Mac, con botones (F40).

ADR-017 cerró el CoT EN VIVO de la Mac: el bus de eventos no acepta
espectadores y el rollout no trae thinking. Pero el historial de la sesión
(session/messages) persiste las parts type=='reasoning' también para las
sesiones interactive de la UI — verificado 20/10: 21k chars de reasoning
en los últimos 12 mensajes de Projects/cv. Así que al CERRAR el turno
(on_stop) el razonamiento completo está ahí.

Flujo (idea de Damian: "que aparezcan cot1 cot2 y elegir cuál ver"):
on_stop spawnea `--cot-botones` (subproceso: el hook no espera el resume)
→ capturar(): runtime de solo lectura, resume, los bloques reasoning del
ÚLTIMO turno → cache en state/cot-<sid>-<t0>.json → un mensaje con un
botón por bloque. El tap (ct:sid:t0:n[:p] → /cotb) LEE EL CACHE: sin
runtime, instantáneo; bloque grande → partes iguales con botones.
"""
import datetime
import json
import os
import time

from zcode_bot import config
from zcode_bot.config import marker_path
from zcode_bot import proto as zc_proto
from zcode_bot.messages import _fetch_messages
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, partes_iguales
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import pwlog

COT_PARTE_CAP = 3000      # chars por parte de un bloque (bajo CH_CAJA: el <pre> suma poco)
COT_BOTONES_MAX = 20      # un botón por bloque; más que eso queda en /cot (documento)
COT_BOTONES_FILA = 5
COT_MSGS = 60             # mensajes a traer: el turno de la Mac más largo entra
COT_RESUME_TIMEOUT_S = 120
ARCH_COT = "cot-%s-%d.json"


def cache_path(sid, t0):
    return os.path.join(config.STATE_DIR, ARCH_COT % (sid, int(t0)))


def bloques_del_turno(msgs):
    """Parts reasoning del ÚLTIMO turno: todo lo posterior al último
    mensaje del usuario, en orden. Estructura real: {info:{role}, parts}."""
    ini = 0
    for i, m in enumerate(msgs):
        rol = (m.get("info") or {}).get("role") or m.get("role")
        if rol == "user":
            ini = i + 1
    out = []
    for m in msgs[ini:]:
        for p in m.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "reasoning":
                t = str(p.get("text") or p.get("reasoning") or "").strip()
                if t:
                    out.append(t)
    return out


def _filas(botones):
    filas, fila = [], []
    for b in botones:
        fila.append(b)
        if len(fila) == COT_BOTONES_FILA:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    return filas


def botones_bloques(sid, t0, n):
    return _filas([{"text": "CoT %d" % i,
                    "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                   for i in range(1, min(n, COT_BOTONES_MAX) + 1)])


def botones_partes(sid, t0, n, total, actual):
    return _filas([{"text": ("[%d/%d]" if p == actual else "%d/%d")
                    % (p, total),
                    "callback_data": "ct:%s:%d:%d:%d" % (sid, int(t0), n, p)}
                   for p in range(1, total + 1)])


def capturar(cfg, sid, titulo, t0, ws):
    """Al cerrar un turno de la Mac: trae el historial, cachea los bloques
    reasoning del turno y manda el mensaje con botones. Devuelve la
    cantidad de bloques (None si no hubo razonamiento o falló)."""
    c = zc_proto.make(cwd=ws)
    try:
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
        if e := zc_proto.proto_err(r, "resume"):
            pwlog("cot-botones %s: %s" % (sid[-8:], e[:120]))
            return None
        msgs = _fetch_messages(c, sid, limit=COT_MSGS) or []
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    bloques = bloques_del_turno(msgs)
    if not bloques:
        return None
    guardar(sid, t0, titulo, bloques)
    total = sum(len(b) for b in bloques)
    hora = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    n = len(bloques)
    texto = ("🧠 <b>Razonamiento</b> de «%s» (turno de las %s) — %d bloque%s, "
             "%d chars. Tocá uno para leerlo:"
             % (esc(titulo or sid[-8:]), hora, n, "s" if n != 1 else "",
                total))
    if n > COT_BOTONES_MAX:
        texto += ("\n(botones para los primeros %d; el documento completo "
                  "con /cot)" % COT_BOTONES_MAX)
    uhttp.send(cfg, texto, loud=False,
               markup={"inline_keyboard": botones_bloques(sid, t0, n)})
    return n


def mostrar(sid, t0, n, parte=1):
    """El tap: bloque n del cache, en partes iguales si es grande."""
    try:
        with open(cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ("🤷 Ese razonamiento ya no está guardado (se purga a los "
                "2 días) — /cot trae el documento completo de la task.")
    bloques = d.get("bloques") or []
    if n < 1 or n > len(bloques):
        return "⚠️ Bloque fuera de rango (1 a %d)." % len(bloques)
    partes = partes_iguales(bloques[n - 1], COT_PARTE_CAP)
    parte = min(max(parte, 1), len(partes))
    cab = "🧠 <b>CoT %d/%d</b> · «%s»" % (n, len(bloques),
                                         esc(d.get("titulo") or sid[-8:]))
    if len(partes) == 1:
        return cab + "\n<pre>%s</pre>" % esc(partes[0])
    cab += " · parte %d/%d" % (parte, len(partes))
    return (cab + "\n<pre>%s</pre>" % esc(partes[parte - 1]),
            {"inline_keyboard": botones_partes(sid, t0, n, len(partes),
                                               parte)})


# F41 — el CoT EN VIVO (diseño de Damian: botones que crecen mientras
# el modelo piensa, cada bloque su botón-referencia, nada de texto largo)
COT_VIVO_CAD_S = 3.0     # cadencia del polling al historial
COT_VIVO_MAX_S = 35 * 60  # techo del vigilante (el mismo del espectador)


def bloques_vivos(msgs):
    """(bloques completos, chars del bloque en curso) del turno en marcha.
    Un reasoning está COMPLETO cuando ya NO es la última part del último
    mensaje: mientras el modelo piensa, el part reasoning del final crece
    (verificado en vivo: 6.7k chars legibles a mitad de paso); cuando el
    paso cierra, le siguen text/tool y el bloque ya no cambia."""
    ini = 0
    for i, m in enumerate(msgs):
        rol = (m.get("info") or {}).get("role") or m.get("role")
        if rol == "user":
            ini = i + 1
    bloques, pendiente = [], 0
    for mi, m in enumerate(msgs[ini:]):
        parts = m.get("parts") or []
        for pi, p in enumerate(parts):
            if not (isinstance(p, dict) and p.get("type") == "reasoning"):
                continue
            txt = str(p.get("text") or p.get("reasoning") or "").strip()
            if not txt:
                continue
            if mi == len(msgs[ini:]) - 1 and pi == len(parts) - 1:
                pendiente = len(txt)
            else:
                bloques.append(txt)
    return bloques, pendiente


def guardar(sid, t0, titulo, bloques, **extra):
    """Escribe/actualiza el cache de un turno (F40 captura, F41 vivo, F42
    celu: LiveTurnView lo actualiza por fase). extra: vivo/finalizado."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(cache_path(sid, t0), "w") as f:
        json.dump({"sid": sid, "titulo": titulo, "t0": int(t0),
                   "bloques": bloques, **extra}, f)


def cache_existe(sid, t0):
    return os.path.exists(cache_path(sid, t0))


def texto_vivo(titulo, n, pendiente):
    linea = "🧠 <b>«%s»</b> pensando… · %d bloque%s listo%s" % (
        esc(titulo or "?"), n, "s" if n != 1 else "", "s" if n != 1 else "")
    if pendiente:
        linea += " · escribiendo uno (%d chars)" % pendiente
    if n:
        linea += "\nTocá un botón para leerlo:"
    return linea


def texto_final(titulo, t0, n, total):
    hora = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    texto = ("🧠 <b>Razonamiento</b> de «%s» (turno de las %s) — %d bloque%s, "
             "%d chars. Tocá uno para leerlo:"
             % (esc(titulo or "?"), hora, n, "s" if n != 1 else "", total))
    if n > COT_BOTONES_MAX:
        texto += ("\n(botones para los primeros %d; el documento completo "
                  "con /cot)" % COT_BOTONES_MAX)
    return texto


def vivir(cfg, sid, titulo, t0, ws):
    """El vigilante del CoT en vivo (subproceso --cot-vivo): caja «pensando…»
    al arrancar, y cada bloque de razonamiento que se completa AGREGA su
    botón editando el teclado del MISMO mensaje. Fin: marker consumido por
    on_stop (= el turno cerró) o techo de 35 min → texto final con el total
    y cache finalizado (los taps siguen andando; /cot también). El cache se
    escribe al ARRANCAR: on_stop salta el --cot-botones de F40 si existe."""
    os.makedirs(config.STATE_DIR, exist_ok=True)

    guardar(sid, t0, titulo, [], vivo=True)  # dueño: F40 no duplica
    ph = uhttp.send(cfg, texto_vivo(titulo, 0, 0), loud=False)
    c = zc_proto.make(cwd=ws)
    try:
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
        if e := zc_proto.proto_err(r, "resume"):
            pwlog("cot-vivo %s: %s" % (sid[-8:], e[:120]))
            guardar(sid, t0, titulo, [], finalizado=True, error=e[:120])
            return
        visto = -1
        t_fin = time.time() + COT_VIVO_MAX_S
        while time.time() < t_fin and os.path.exists(marker_path(sid)):
            msgs = _fetch_messages(c, sid, limit=COT_MSGS) or []
            bloques, pend = bloques_vivos(msgs)
            if len(bloques) != visto:
                guardar(sid, t0, titulo, bloques, vivo=True)
                uhttp._live_edit(cfg, ph, texto_vivo(titulo, len(bloques),
                                                      pend),
                                 markup={"inline_keyboard":
                                         botones_bloques(sid, t0,
                                                         len(bloques))})
                visto = len(bloques)
            time.sleep(COT_VIVO_CAD_S)
        # turno cerrado: un fetch más y el texto final con TODOS los bloques
        msgs = _fetch_messages(c, sid, limit=COT_MSGS) or []
        bloques, _pend = bloques_vivos(msgs)
        guardar(sid, t0, titulo, bloques, finalizado=True)
        total = sum(len(b) for b in bloques)
        uhttp._live_edit(cfg, ph, texto_final(titulo, t0, len(bloques),
                                               total),
                         markup={"inline_keyboard":
                                 botones_bloques(sid, t0, len(bloques))})
        pwlog("cot-vivo fin: bloques=%d" % len(bloques))
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def cmd_cot_vivo(sid, titulo, t0, ws):
    """Entrypoint del subproceso detached (gateway --cot-vivo)."""
    cfg = load_config()
    try:
        vivir(cfg, sid, titulo, float(t0), ws)
    except Exception as exc:  # noqa: BLE001 - el subproceso reporta, no muere
        pwlog("cot-vivo %s falló: %s" % (sid[-8:], exc))


def cmd_cot_botones(sid, titulo, t0, ws):
    """Entrypoint del subproceso detached (gateway --cot-botones)."""
    cfg = load_config()
    try:
        k = capturar(cfg, sid, titulo, float(t0), ws)
        pwlog("cot-botones %s: %s bloques" % (sid[-8:], k))
    except Exception as exc:  # noqa: BLE001 - el subproceso reporta, no muere
        pwlog("cot-botones %s falló: %s" % (sid[-8:], exc))
