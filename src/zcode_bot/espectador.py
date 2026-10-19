"""espectador — los turnos de la Mac en vivo por Telegram (F33, ADR-017).

on_user_prompt spawnea este proceso desprendido para la task current sin
worker del bot (F15: el worker ya streamea lo suyo). Mientras el turno
corre en la app, tail incremental del rollout model-io-<sid>.jsonl (el
mismo archivo que F31 lee para /ctx: un registro por request completado)
y por cada paso nuevo manda a Telegram: caja de tool call (comando
completo / diff real, el formato de messages._tool_caja) y narración
intermedia. La respuesta FINAL jamás la manda el espectador (solo pasos
con finishReason tool-calls): la entrega on_stop/F32 — cero duplicación
por construcción.

Por qué NO session/events: el bus de eventos es del cliente que maneja
el turno — "Session is not active" (-32004) para cualquier otro (spike
19/10: tercera señal, ver ADR-017). El rollout es puro archivo: sin
runtime, sin protocolo, sin cliente.

CoT: no disponible para turnos de la app — el runtime no persiste los
thinking blocks ni en el bus ni en el rollout (spike 19/10). Si algún
día aparecen, _procesa es el lugar.

Espera reactiva: fswatch sobre el rollout + el marker del turno (el
mismo wait_change del worker); el marker consumido es la señal de fin
(orquestación por archivos, como siempre). Cap duro 35 min (app muerta
sin Stop hook). Testeable por deps inyectadas (send/sleep/now/marker/
wait) — sin red, sin time.sleep global.
"""
import datetime
import json
import os
import time

from zcode_bot import audit
from zcode_bot import config
from zcode_bot import fswatch
from zcode_bot.hooks import marker_path
from zcode_bot.messages import _tool_caja, _tool_caja_on
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, task_label
from zcode_bot.utils.logging import pwlog

CAP_S = 35 * 60      # cap duro del espectador (stuck-watch vive 20)
REG_CAP = 8 << 20    # un registro mayor se salta (avanzar el offset igual)
PACE_CADA = 3        # cada N envíos, dormir 1 s (~1 msg/s del chat)


def rollout_path(sid):
    return os.path.join(config.BASE, "cli", "rollout",
                        "model-io-%s.jsonl" % sid)


def _leer_nuevos(p, pos):
    """Registros COMPLETOS nuevos desde el offset → (lista, pos_nueva).
    La última línea sin \\n puede estar a mitad de escritura: queda para
    la próxima vuelta (el offset no la consume). Jamás lee el archivo
    entero: solo los bytes nuevos."""
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            total = f.tell()
            if total <= pos:
                return [], pos
            f.seek(pos)
            data = f.read(min(total - pos, 64 << 20))
    except OSError:
        return [], pos
    if not data.endswith(b"\n"):
        corte = data.rfind(b"\n")
        if corte < 0:
            return [], pos
        data = data[:corte + 1]
    out = []
    for ln in data.splitlines():
        if len(ln) > REG_CAP:
            continue  # monstruo: se avanza igual (el offset ya corrió)
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out, pos + len(data)


def _ts_ms(ts):
    """startedAt del rollout → epoch ms. VIENE COMO STRING ISO con Z
    (bug del F33 original: isinstance((int, float)) nunca daba True con
    el formato real → correlación muerta → espectador mudo). Acepta
    números por si el runtime cambia de formato."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        try:
            return datetime.datetime.fromisoformat(
                ts.replace("Z", "+00:00")).timestamp() * 1000.0
        except ValueError:
            return None
    return None


def _procesa(reg, st, enviar):
    """Un registro del rollout → mensajes del chat. st es el estado entre
    registros (turn_id, t0_ms, paso, titulo, cfg, avisado). Correlación
    F20: el primer registro nacido tras el arranque fija EL turno; los
    de otros turnIds (o del compact) no existen para el espectador."""
    if str(reg.get("querySource") or "") != "main_turn":
        return 0
    ts0 = _ts_ms(reg.get("startedAt"))
    if st["turn_id"] is None:
        if ts0 is not None and ts0 >= st["t0_ms"] - 1000:
            st["turn_id"] = str(reg.get("turnId") or "")
            st.get("log") and st["log"]("espectador: turno fijado %s"
                                        % st["turn_id"][:18])
        else:
            return 0  # registro viejo del archivo (turno anterior)
    elif str(reg.get("turnId") or "") != st["turn_id"]:
        return 0
    resp = reg.get("response") or {}
    fin = str(resp.get("finishReason") or "")
    n = 0
    texto = str(resp.get("text") or "")
    if texto and fin == "tool-calls":
        # narración intermedia; la FINAL (finishReason stop) la entrega F32
        n += _avisar(st, enviar,
                     "🖥️ <b>«%s»</b>\n%s" % (esc(st["titulo"]),
                                             esc(texto[:2500])))
    for tc in resp.get("toolCalls") or []:
        if not isinstance(tc, dict):
            continue
        if not st["cfg"].get("tools_stream", True):
            break  # knob apaga las cajas (narración sigue)
        nombre = str(tc.get("name") or "tool")
        inp = tc.get("input") if isinstance(tc.get("input"), dict) else {}
        if not _tool_caja_on(nombre, st["cfg"]):
            continue
        st["paso"] += 1
        n += _avisar(st, enviar, "%s · <b>%d</b>"
                     % (_tool_caja(nombre, inp), st["paso"]))
    return n


def _avisar(st, enviar, texto):
    """Primer mensaje del turno lleva el rótulo de espectador; turnos
    sin pasos no generan ruido (nada de header vacío)."""
    if not st["avisado"]:
        st["avisado"] = True
        enviar("🖥️ <b>«%s»</b> — turno de la Mac en vivo (espectador)"
               % esc(st["titulo"]))
    enviar(texto)
    return 1


def mirar(sid, cwd, cfg, deps=None):
    """Loop del espectador: tail reactivo del rollout hasta que el marker
    del turno desaparezca (on_stop lo consumió → F32 tiene la posta) o
    venza el cap. Devuelve la cantidad de pasos vistos."""
    d = deps or {}
    enviar_raw = d.get("send", uhttp.send)
    dormir = d.get("sleep", time.sleep)
    ahora = d.get("now", time.time)
    marker_vivo = d.get("marker_vivo",
                        lambda: os.path.exists(marker_path(sid)))
    esperar = d.get("wait", fswatch.wait_change)

    def enviar(texto):
        enviar_raw(cfg, texto, loud=False)

    st = {"turn_id": None, "t0_ms": (ahora() - 1.0) * 1000.0, "paso": 0,
          "titulo": task_label({"sessionId": sid, "cwd": cwd}),
          "cfg": cfg, "avisado": False, "log": pwlog}
    tope = ahora() + CAP_S
    p = rollout_path(sid)
    pos = 0
    try:
        pos = os.path.getsize(p)
    except OSError:
        pass
    # el rollout puede no existir aún (sesión nueva): mirar la carpeta
    # hasta que aparezca; el marker entra en la misma espera (fin reactivo)
    paths = [p if os.path.exists(p) else os.path.dirname(p), marker_path(sid)]
    while ahora() < tope:
        if not marker_vivo():
            break
        enviados = 0
        regs, pos = _leer_nuevos(p, pos)
        for reg in regs:
            try:
                enviados += _procesa(reg, st, enviar)
            except Exception as exc:  # noqa: BLE001 — ver, no romper
                audit.registrar(sid, "espectador", str(exc)[:150])
        if enviados >= PACE_CADA:
            dormir(1.0)  # pace ~1 msg/s del chat en ráfagas
        esperar(paths, timeout=max(0.05, min(2.0, tope - ahora())))
    pwlog("espectador fin: pasos=%d" % st["paso"])
    return st["paso"]


def cmd_espectador(sid, cwd):
    mirar(sid, cwd, load_config())
