"""spectator — the Mac's turns live via Telegram (F33, ADR-017).

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
from zcode_bot import agent_live  # F63: subagent label handoff
from zcode_bot.core import _task_current_ok
from zcode_bot.hooks import marker_path
from zcode_bot.messages import _tool_box, _tool_box_on
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, task_label
from zcode_bot.utils.logging import pwlog

CAP_S = 35 * 60      # hard cap of the spectator (stuck-watch lives at 20)
REG_CAP = 8 << 20    # a larger record is skipped (advance the offset anyway)
PACE_EVERY = 3       # every N sends, sleep 1 s (~1 msg/s of the chat)
SPEC_PID_FMT = "spec-%s.pid"   # F62: single-flight (the attach may respawn)


def spec_pid_path(sid: str) -> str:
    return os.path.join(config.STATE_DIR, SPEC_PID_FMT % sid)


def _marker_token(sid: str) -> str:
    """The in-flight turn's token (unique per prompt) — "" without a
    marker or mid-write."""
    try:
        with open(marker_path(sid)) as f:
            return str((json.load(f) or {}).get("token") or "")
    except (OSError, ValueError):
        return ""


def alive(sid: str, proc_alive=None) -> bool:
    """F62 — is a spectator of THIS TURN already running? A live pid
    whose pid-file is at least as new as the current marker (25/10: a
    spectator from the PREVIOUS turn — pinned to its own turnId — was
    alive 30 min later; the single-flight then skipped the new turn's
    spawn and the turn went mute). Dead or older → False: the hook/attach
    spawns a fresh one and takes the pid-file."""
    try:
        with open(spec_pid_path(sid)) as f:
            pid = int(f.read().strip() or 0)
        pid_mt = os.path.getmtime(spec_pid_path(sid))
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if pid_mt < os.path.getmtime(marker_path(sid)) - 1.0:
            return False
    except OSError:
        pass  # silent-by-design: probe/read, the except is the verdict
    if proc_alive is None:
        from zcode_bot.workers import _proc_alive  # lazy: workers → live_view
        proc_alive = _proc_alive
    return bool(proc_alive(pid))


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
            st.get("log") and st["log"]("spectator: turn pinned %s"
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
        # F63: the parent spawning a subagent — hand the label to the
        # agent watcher (before the box, so the agent is born instantly)
        if name in agent_live.AGENT_TOOLS:
            agent_live.note_spawn(st["sid"], name, inp)
            continue
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
    # F71 — the F15 gate read LIVE on every push (and the F67 view mute,
    # missed here): a Mac turn keeps running after the user switches
    # tasks and its tool boxes kept landing over whatever they were
    # doing (26/10: the zcode_bot turn's boxes over the diff_ask task)
    may_push = d.get("may_push",
                     lambda: _task_current_ok(cfg, sid)
                     and not agent_live.view_alive(sid))

    def send_msg(text):
        if may_push():
            send_raw(cfg, text, loud=False)

    st = {"turn_id": None, "t0_ms": (now() - 1.0) * 1000.0, "step": 0,
          "sid": sid,
          "title": task_label({"sessionId": sid, "cwd": cwd}),
          "cfg": cfg, "noticed": False, "log": pwlog}
    # F62: take the pid-file — a second spawn (the attach racing a new
    # prompt) must see us and stay down, or the chat gets every step twice
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(spec_pid_path(sid), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass  # silent-by-design: probe/read, the except is the verdict
    try:
        deadline = now() + CAP_S
        p = config.rollout_path(sid)
        pos = 0
        try:
            pos = os.path.getsize(p)
        except OSError:
            pass  # silent-by-design: probe/read, the except is the verdict
        # the rollout may not exist yet (new session): watch the folder
        # until it appears; the marker joins the same wait (reactive end)
        paths = [p if os.path.exists(p) else os.path.dirname(p), marker_path(sid)]
        token0 = _marker_token(sid)
        while now() < deadline:
            if not marker_alive():
                break
            # a NEW prompt re-created the marker: that is another turn
            # (its own spectator is spawned by the hook) — step down
            # instead of lingering pinned to a turnId nobody runs
            tok = _marker_token(sid)
            if token0 and tok and tok != token0:
                pwlog("spectator: turn changed — stepping down")
                break
            if not token0 and tok:
                token0 = tok
            sent = 0
            records, pos = _read_new(p, pos)
            for record in records:
                try:
                    sent += _process(record, st, send_msg)
                except Exception as exc:  # noqa: BLE001 — watch, don't break
                    audit.record(sid, "spectator", str(exc)[:150])
            if sent >= PACE_EVERY:
                sleep(1.0)  # pace ~1 msg/s of the chat in bursts
            wait(paths, timeout=max(0.05, min(2.0, deadline - now())))
        pwlog("spectator end: steps=%d" % st["step"])
        return st["step"]
    finally:
        try:
            os.remove(spec_pid_path(sid))
        except OSError:
            pass  # silent-by-design: cleanup/close, best-effort


def cmd_spectator(sid, cwd):
    watch(sid, cwd, load_config())