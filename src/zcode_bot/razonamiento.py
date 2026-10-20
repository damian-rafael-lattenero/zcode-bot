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

from zcode_bot import config
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
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(cache_path(sid, t0), "w") as f:
        json.dump({"sid": sid, "titulo": titulo, "t0": int(t0),
                   "bloques": bloques}, f)
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


def cmd_cot_botones(sid, titulo, t0, ws):
    """Entrypoint del subproceso detached (gateway --cot-botones)."""
    cfg = load_config()
    try:
        k = capturar(cfg, sid, titulo, float(t0), ws)
        pwlog("cot-botones %s: %s bloques" % (sid[-8:], k))
    except Exception as exc:  # noqa: BLE001 - el subproceso reporta, no muere
        pwlog("cot-botones %s falló: %s" % (sid[-8:], exc))
