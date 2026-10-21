"""espectador — the Mac's turns live via Telegram (F33, ADR-017).

on_user_prompt spawns this detached process for the current task
without a bot worker (F15: the worker already streams its own). While
the turn runs in the app, incremental tail of the rollout
model-io-<sid>.jsonl (the same file F31 reads for /ctx: one record per
completed request) and for each new step it sends to Telegram: a tool
call box (full command / real diff, the messages._tool_caja format)
and intermediate narration. The FINAL answer is never sent by the
spectator (only steps with finishReason tool-calls): on_stop/F32
delivers it — zero duplication by construction.

Why NOT session/events: the event bus belongs to the client handling
the turn — "Session is not active" (-32004) for anyone else (spike
19/10: third signal, see ADR-017). The rollout is pure file: no
runtime, no protocol, no client.

CoT: not available for app turns — the runtime does not persist the
thinking blocks either in the bus or in the rollout (spike 19/10). If
they ever appear, _procesa is the place.

Reactive wait: fswatch on the rollout + the turn's marker (the same
wait_change as the worker); the consumed marker is the end signal
(file-based orchestration, as always). Hard cap 35 min (app dead
without a Stop hook). Testable via injected deps (send/sleep/now/
marker/wait) — no network, no global time.sleep.
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

CAP_S = 35 * 60      # hard cap of the spectator (stuck-watch lives at 20)
REG_CAP = 8 << 20    # a larger record is skipped (advance the offset anyway)
PACE_CADA = 3        # every N sends, sleep 1 s (~1 msg/s of the chat)


def _leer_nuevos(p, pos):
    """New COMPLETE records since the offset → (list, new_pos). The last
    line without \\n may be mid-write: it stays for the next round (the
    offset does not consume it). Never reads the whole file: only the
    new bytes."""
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
            continue  # monster record: advance anyway (the offset already moved)
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out, pos + len(data)


def _ts_ms(ts):
    """startedAt of the rollout → epoch ms. IT COMES AS AN ISO STRING
    with Z (bug of the original F33: isinstance((int, float)) was never
    True with the real format → dead correlation → mute spectator).
    Accepts numbers in case the runtime changes format."""
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
    """One rollout record → chat messages. st is the state between
    records (turn_id, t0_ms, paso, titulo, cfg, avisado). F20
    correlation: the first record born after startup pins THE turn;
    those from other turnIds (or from the compact) do not exist for
    the spectator."""
    if str(reg.get("querySource") or "") != "main_turn":
        return 0
    ts0 = _ts_ms(reg.get("startedAt"))
    if st["turn_id"] is None:
        if ts0 is not None and ts0 >= st["t0_ms"] - 1000:
            st["turn_id"] = str(reg.get("turnId") or "")
            st.get("log") and st["log"]("espectador: turno fijado %s"
                                        % st["turn_id"][:18])
        else:
            return 0  # old record from the file (previous turn)
    elif str(reg.get("turnId") or "") != st["turn_id"]:
        return 0
    resp = reg.get("response") or {}
    fin = str(resp.get("finishReason") or "")
    n = 0
    texto = str(resp.get("text") or "")
    if texto and fin == "tool-calls":
        # intermediate narration; the FINAL one (finishReason stop) is delivered by F32
        n += _avisar(st, enviar,
                     "🖥️ <b>«%s»</b>\n%s" % (esc(st["titulo"]),
                                             esc(texto[:2500])))
    for tc in resp.get("toolCalls") or []:
        if not isinstance(tc, dict):
            continue
        if not st["cfg"].get("tools_stream"):
            break  # knob turns the boxes off (narration continues)
        nombre = str(tc.get("name") or "tool")
        inp = tc.get("input") if isinstance(tc.get("input"), dict) else {}
        if not _tool_caja_on(nombre, st["cfg"]):
            continue
        st["paso"] += 1
        n += _avisar(st, enviar, "%s · <b>%d</b>"
                     % (_tool_caja(nombre, inp), st["paso"]))
    return n


def _avisar(st, enviar, texto):
    """The turn's first message carries the spectator label; turns
    without steps make no noise (no empty header)."""
    if not st["avisado"]:
        st["avisado"] = True
        enviar("🖥️ <b>«%s»</b> — turno de la Mac en vivo (espectador)"
               % esc(st["titulo"]))
    enviar(texto)
    return 1


def mirar(sid, cwd, cfg, deps=None):
    """The spectator loop: reactive tail of the rollout until the turn's
    marker disappears (on_stop consumed it → F32 has the real thing) or
    the cap expires. Returns the number of steps seen."""
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
    p = config.rollout_path(sid)
    pos = 0
    try:
        pos = os.path.getsize(p)
    except OSError:
        pass
    # the rollout may not exist yet (new session): watch the folder
    # until it appears; the marker joins the same wait (reactive end)
    paths = [p if os.path.exists(p) else os.path.dirname(p), marker_path(sid)]
    while ahora() < tope:
        if not marker_vivo():
            break
        enviados = 0
        regs, pos = _leer_nuevos(p, pos)
        for reg in regs:
            try:
                enviados += _procesa(reg, st, enviar)
            except Exception as exc:  # noqa: BLE001 — watch, don't break
                audit.registrar(sid, "espectador", str(exc)[:150])
        if enviados >= PACE_CADA:
            dormir(1.0)  # pace ~1 msg/s of the chat in bursts
        esperar(paths, timeout=max(0.05, min(2.0, tope - ahora())))
    pwlog("espectador fin: pasos=%d" % st["paso"])
    return st["paso"]


def cmd_espectador(sid, cwd):
    mirar(sid, cwd, load_config())
