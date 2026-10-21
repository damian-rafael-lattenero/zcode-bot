"""espectador — the Mac's turns live via Telegram (F33, ADR-017).

on_user_prompt spawns this detached process for the current task
without a bot worker (F15: the worker already streams its own). While
the turn runs in the app, incremental tail of the rollout
model-io-<sid>.jsonl (the same file F31 reads for /ctx: one record per
completed request) and for each new step it sends to Telegram: a tool
call box (full command / real diff, the messages._tool_box format)
and intermediate narration. The FINAL answer is never sent by the
spectator (only steps with finishReason tool-calls): on_stop/F32
delivers it — zero duplication by construction.

Why NOT session/events: the event bus belongs to the client handling
the turn — "Session is not active" (-32004) for anyone else (spike
19/10: third signal, see ADR-017). The rollout is pure file: no
runtime, no protocol, no client.

CoT: not available for app turns — the runtime does not persist the
thinking blocks either in the bus or in the rollout (spike 19/10). If
they ever appear, _process is the place.

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
from zcode_bot.messages import _tool_box, _tool_box_on
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, task_label
from zcode_bot.utils.logging import pwlog

CAP_S = 35 * 60      # hard cap of the spectator (stuck-watch lives at 20)
REG_CAP = 8 << 20    # a larger record is skipped (advance the offset anyway)
PACE_EVERY = 3       # every N sends, sleep 1 s (~1 msg/s of the chat)


def _read_new(p, pos):
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
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], pos
        data = data[:cut + 1]
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


def _process(record, st, send_msg):
    """One rollout record → chat messages. st is the state between
    records (turn_id, t0_ms, step, title, cfg, noticed). F20
    correlation: the first record born after startup pins THE turn;
    those from other turnIds (or from the compact) do not exist for
    the spectator."""
    if str(record.get("querySource") or "") != "main_turn":
        return 0
    ts0 = _ts_ms(record.get("startedAt"))
    if st["turn_id"] is None:
        if ts0 is not None and ts0 >= st["t0_ms"] - 1000:
            st["turn_id"] = str(record.get("turnId") or "")
            st.get("log") and st["log"]("espectador: turno fijado %s"
                                        % st["turn_id"][:18])
        else:
            return 0  # old record from the file (previous turn)
    elif str(record.get("turnId") or "") != st["turn_id"]:
        return 0
    resp = record.get("response") or {}
    finish = str(resp.get("finishReason") or "")
    n = 0
    text = str(resp.get("text") or "")
    if text and finish == "tool-calls":
        # intermediate narration; the FINAL one (finishReason stop) is delivered by F32
        n += _notify(st, send_msg,
                     "🖥️ <b>«%s»</b>\n%s" % (esc(st["title"]),
                                             esc(text[:2500])))
    for tc in resp.get("toolCalls") or []:
        if not isinstance(tc, dict):
            continue
        if not st["cfg"].get("tools_stream"):
            break  # knob turns the boxes off (narration continues)
        name = str(tc.get("name") or "tool")
        inp = tc.get("input") if isinstance(tc.get("input"), dict) else {}
        if not _tool_box_on(name, st["cfg"]):
            continue
        st["step"] += 1
        n += _notify(st, send_msg, "%s · <b>%d</b>"
                     % (_tool_box(name, inp), st["step"]))
    return n


def _notify(st, send_msg, text):
    """The turn's first message carries the spectator label; turns
    without steps make no noise (no empty header)."""
    if not st["noticed"]:
        st["noticed"] = True
        send_msg("🖥️ <b>«%s»</b> — Mac turn live (spectator)"
                 % esc(st["title"]))
    send_msg(text)
    return 1


def watch(sid, cwd, cfg, deps=None):
    """The spectator loop: reactive tail of the rollout until the turn's
    marker disappears (on_stop consumed it → F32 has the real thing) or
    the cap expires. Returns the number of steps seen."""
    d = deps or {}
    send_raw = d.get("send", uhttp.send)
    sleep = d.get("sleep", time.sleep)
    now = d.get("now", time.time)
    marker_alive = d.get("marker_alive",
                         lambda: os.path.exists(marker_path(sid)))
    wait = d.get("wait", fswatch.wait_change)

    def send_msg(text):
        send_raw(cfg, text, loud=False)

    st = {"turn_id": None, "t0_ms": (now() - 1.0) * 1000.0, "step": 0,
          "title": task_label({"sessionId": sid, "cwd": cwd}),
          "cfg": cfg, "noticed": False, "log": pwlog}
    deadline = now() + CAP_S
    p = config.rollout_path(sid)
    pos = 0
    try:
        pos = os.path.getsize(p)
    except OSError:
        pass
    # the rollout may not exist yet (new session): watch the folder
    # until it appears; the marker joins the same wait (reactive end)
    paths = [p if os.path.exists(p) else os.path.dirname(p), marker_path(sid)]
    while now() < deadline:
        if not marker_alive():
            break
        sent = 0
        records, pos = _read_new(p, pos)
        for record in records:
            try:
                sent += _process(record, st, send_msg)
            except Exception as exc:  # noqa: BLE001 — watch, don't break
                audit.registrar(sid, "espectador", str(exc)[:150])
        if sent >= PACE_EVERY:
            sleep(1.0)  # pace ~1 msg/s of the chat in bursts
        wait(paths, timeout=max(0.05, min(2.0, deadline - now())))
    pwlog("espectador fin: pasos=%d" % st["step"])
    return st["step"]


def cmd_spectator(sid, cwd):
    watch(sid, cwd, load_config())
