"""workers — the bot's headless turns (F4g).

The queue (bot-queue.json), spawning workers as detached PROCESSES
(F16), prompt_worker (the heart: session → runtime → live view →
delivery), the orphan handoff and bot_stop. prompt_worker remains a
separate PROCESS (real isolation against /nuclear); spawning is a
direct subprocess. Transport VIA ATTRIBUTE (uhttp.*).

THE Mac↔phone MODEL (F51, decided by Damian 21/10): ONE session shared
with the Mac, turns SERIALIZED through this queue (running_turns +
registry: if the Mac is running, the message waits; when it frees the
session, it goes out on its own — full continuity in both directions).
No automatic fork or merge: the phone extends the Mac's conversation.
/stop only reaches bot turns — stopping the Mac is the app's Stop
button or /nuclear.
"""
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from zcode_bot import brain_client  # ADR-020
from zcode_bot import bus  # ADR-019
from zcode_bot import config
from zcode_bot import proto as zc_proto
from zcode_bot import acks
from zcode_bot import cli_logs as zc_logs
from zcode_bot import audit
from zcode_bot.schemas import QueueItemShape, WorkersEntry
from zcode_bot.state.registry import entry_title
from zcode_bot.core import (BotSession, active_bot_session,
                            set_active_bot_session,
                            set_worker_session,
                            _store, _workers, _questions,
                            _stop_request, _stop_requested, _stop_consume,
                            _interrupt_request, _interrupt_pending,
                            _interrupt_consume,
                            _task_current_ok,
                            _perm_key, _perm_replay, _perm_remember,
                            _model_snapshot, _restore_model, _proto_ready,
                            _create_session, _save_last_prompt,
                            _wait_cli_event,
                            transcript_mb, _compact_timeout,
                            running_turns, force_turn_stopped,
                            _queue_peek, _queue_sids_with_items)
from zcode_bot.live_view import (LiveTurnView, _turn_end_poll, _headless_text,
                                 _rss_mb, _rss_now_mb)
from zcode_bot import reasoning as cotmod
from zcode_bot import verbosity  # F61
from zcode_bot import agent_live  # F63
from zcode_bot.messages import (_fetch_messages, _extract_assistant_text,
                                _last_id_of)
from zcode_bot.reporting import _turn_trace, _send_long_reply
from zcode_bot.nuclear import _nuclear_active
from zcode_bot.models import MODEL_SEL
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log, log_exc, pwlog


@dataclass
class QueueItem:
    """A queued prompt (bot-queue.json). The file persists the historical
    shape {"act", "text"} + ts (F30, stamped in _queue_put); in memory
    it is typed with a compat mapping — item["text"] and item.get("act")
    still work. Legacy items without ts: the auditor does not flag them
    (no honest data, no warning)."""
    act: Optional[Dict[str, Any]] = None
    text: str = ""
    ts: float = 0.0

    def to_dict(self) -> QueueItemShape:
        return {"act": self.act or {}, "text": self.text, "ts": self.ts}

    @classmethod
    def from_dict(cls, d: Any) -> "QueueItem":
        d = d if isinstance(d, dict) else {}
        return cls(act=d.get("act") if isinstance(d.get("act"), dict) else {},
                   text=str(d.get("text") or ""),
                   ts=float(d.get("ts") or 0.0))

    def keys(self) -> Tuple[str, str, str]:
        return ("act", "text", "ts")

    def __getitem__(self, k: str) -> Any:
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def get(self, k: str, default: Any = None) -> Any:
        return getattr(self, k, default)


@dataclass
class WorkerEntry:
    """Record of a live worker (state/bot-workers.json, flock).
    prompt_worker builds it and persists it with to_dict(); readers
    (status, nuclear, stop) keep using .get via the compat mapping."""
    t0: float = 0.0
    prompt: str = ""
    ws: str = ""
    title: str = ""
    pid: int = 0

    def to_dict(self) -> WorkersEntry:
        return {"t0": self.t0, "prompt": self.prompt, "ws": self.ws,
                "title": self.title, "pid": self.pid}

    @classmethod
    def from_dict(cls, d: Any) -> "WorkerEntry":
        d = d if isinstance(d, dict) else {}
        return cls(t0=float(d.get("t0") or 0.0),
                   prompt=str(d.get("prompt") or ""),
                   ws=str(d.get("ws") or ""),
                   title=str(d.get("title") or d.get("titulo") or ""),  # noqa: spanish — legacy persisted key, permanent bilingual read
                   pid=int(d.get("pid") or 0))

    def get(self, k: str, default: Any = None) -> Any:
        return getattr(self, k, default)


def _proc_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# F39 — honest /stop: worker phase and our own verification of the kill
_STOP_STARTUP_MAX_S = 600  # a worker starting up longer than this = hung: killed all the same
_STOP_VERIFY_S = 2.0       # how long to wait for death after the kill -9 to confirm it
_STOP_VERIFY_STEP_S = 0.2
_PS_TIMEOUT_S = 5
_WARM_STEP_S = 1.0         # warm keepalive poll step (tests shrink it: same loop, faster ticks)


def _proc_dead(pid):
    """The /stop verdict after the kill. After SIGKILL the worker stays a
    ZOMBIE until the poller (its parent) reaps it in the supervisor — and
    os.kill(pid, 0) counts a zombie as alive: to the user a zombie is
    already dead. Nonexistent pid or ps stat 'Z' → dead."""
    if not _proc_alive(pid):
        return True
    try:
        out = (subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                              capture_output=True, text=True,
                              timeout=_PS_TIMEOUT_S).stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return (not out) or out.startswith("Z")


def _cmdline_has(pid, flag):
    """Does the pid run our shim (zcode-telegram.py) with this flag? A
    recycled pid can be any process — even a host runtime, which is
    never touched."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True,
                             timeout=_PS_TIMEOUT_S).stdout or ""
        return "zcode-telegram.py" in out and flag in out
    except (OSError, subprocess.SubprocessError):
        return False


def _run_turn_ok(pid):
    """Safety BEFORE a killpg: verify the pid is one of OUR workers
    (zcode-telegram.py --run-turn)."""
    return _cmdline_has(pid, "--run-turn")


def _cot_live_ok(pid):
    """F44: same for a hot CoT watcher (its pid-file cot-live-*.pid can be
    left orphaned after a kill -9 and point to a recycled pid).
    PERMANENT dual check (P6): pre-deploy watchers still carry the OLD
    --cot-vivo flag in their cmdline — they must remain recognizable so
    /nuclear and the kill guards can reap them; the old flag is no
    longer spawned, but it is matched forever."""
    return _cmdline_has(pid, "--cot-live") or _cmdline_has(pid, "--cot-vivo")


def _kill_runtime(pid):
    """SIGKILL to the worker's GROUP. The spawn uses start_new_session=True
    → the group is worker + child app-server: the whole runtime in one go."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except OSError:
        return False


def bot_stop(sid: str, title: Optional[str] = None,
             grace: float = 4.0,
             cfg: Optional[Dict[str, Any]] = None) -> str:
    """Stops a run started FROM the phone AND clears its queue (F17
    request: /stop also discards that task's queued messages). F16: the
    stop goes through a marker — the WORKER sees it in its loop and runs
    session/stop with its own runtime (a real stop).
    F7++: session/stop is COOPERATIVE — the turn may keep running for
    several seconds (tool in flight, generation half done). If the
    worker doesn't die within `grace` s, we escalate to SIGKILL of the
    whole group. The corpse is picked up by the orphans check and
    reported on its own."""
    lst = _queue_take(sid)
    n = len(lst)
    stopped_title = _workers().flag_stop(sid)
    if stopped_title is None:
        if sid in running_turns():
            # F58 — the ghost (22/10 11:13-11:19): the CLI log says a turn
            # runs in this session but NO worker owns it (it died by force
            # without a CLI end). Free the busy state and give the queued
            # messages back their runner — before, they sat in a black
            # hole while /stop answered «no longer exists».
            force_turn_stopped(sid)
            for it in lst[1:]:
                _queue_put(sid, it.get("act") or {}, it.get("text", ""))
            if lst and cfg is not None and _spawn_worker(
                    cfg, lst[0].get("act") or {},
                    lst[0].get("text", "")) is not None:
                return ("🧟 «%s» was listed busy but nothing was running "
                        "(its turn died unlogged) — ghost cleared, your "
                        "message relaunched." % esc(title or sid[-8:]))
            if lst:
                for it in lst:  # no launch: nothing is lost
                    _queue_put(sid, it.get("act") or {}, it.get("text", ""))
                return ("🧟 «%s» was listed busy but nothing was running — "
                        "ghost cleared; your %d message%s stay queued."
                        % (esc(title or sid[-8:]), n,
                           "" if n == 1 else "s"))
            return ("🧟 «%s» was listed busy but nothing was running (its "
                    "turn died unlogged) — ghost cleared."
                    % esc(title or sid[-8:]))
        if n:
            return ("🧹 Cleared the queue of «%s» (%d messages) — there was "
                    "no bot run to stop."
                    % (esc(title or sid[-8:]), n))
        return ("🤔 That run no longer exists (it ended or was not started "
                "from the phone).")
    _stop_request(sid)
    # F47c: the in-flight turn is marked as stopped ON REQUEST — if the
    # worker dies finished off (grace expired → kill -9, no finally), the
    # orphan supervisor counts it as /stop, not as a failure
    _active_turn_mark(sid, stop=time.time())
    extra = (" · 🧹 queue cleared (%d)" % n) if n else ""
    ent = _workers().get(sid)
    try:
        pid = int((ent or {}).get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 1 and _run_turn_ok(pid):
        # F39: which phase is the worker in? bot-active.json is written when
        # THE TURN STARTS (_active_turn_register); before that the worker
        # is starting the runtime (resume: ~2 min on large sessions) and
        # does NOT look at the marker until it finishes — killing it there
        # was the '💀 no frenó en 4s' of 20/10, on top of that with no  # noqa: spanish — quoted incident banner (20/10)
        # possible report (the orphans check only sees what's registered
        # in bot-active).
        active = _store("bot-active.json").read()
        in_turn = isinstance(active, dict) and sid in active
        try:
            t0w = float((ent or {}).get("t0") or 0) or time.time()
        except (TypeError, ValueError):
            t0w = time.time()
        if not in_turn and time.time() - t0w < _STOP_STARTUP_MAX_S:
            # it stops itself once startup finishes, without running the prompt
            # ('⏹️ frenado por /stop (antes de enviar)', prompt_worker)  # noqa: spanish — quoted old banner
            extra += (" · ⏳ the runtime is starting (session resume, can "
                      "take ~2 min): it stops itself once startup "
                      "finishes, without running your prompt — the "
                      "worker confirms it")
            return "🛑 Stopping «%s»…%s" % (esc(stopped_title or title or sid[-8:]),
                                           extra)
        t0 = time.time()
        while time.time() - t0 < grace and _proc_alive(pid) \
                and _workers().get(sid) is not None:
            time.sleep(0.4)
        if _workers().get(sid) is not None and _proc_alive(pid):
            if _kill_runtime(pid):
                # F58: SIGKILL means the turn end will NEVER reach the
                # CLI log — tombstone it or the session turns into a ghost
                force_turn_stopped(sid)
                # F39: verify the death HERE, without promising someone else's reports
                t1 = time.time()
                while (time.time() - t1 < _STOP_VERIFY_S
                       and not _proc_dead(pid)):
                    time.sleep(_STOP_VERIFY_STEP_S)
                verdict = ("✅ verified: process dead, nothing running"
                           if _proc_dead(pid) else
                           "⚠️ the process is still alive — check "
                           "/status in a moment")
                extra += (" · 💀 did not stop in %.0fs: kill -9 of the "
                          "runtime · %s" % (grace, verdict))
                if in_turn:
                    extra += (" · the turn's close arrives separately "
                              "(orphan check)")
                # the worker won't reach its finally: consume the
                # marker here or the session's NEXT turn would abort on its own
                _stop_consume(sid)
    return "🛑 Stopping «%s»…%s" % (esc(stopped_title or title or sid[-8:]), extra)


def _interrupt_queue_item(cfg, sid, idx, title=None):
    """F58 — interrupt-and-redirect (Damian's main flow, 22/10: «trabajo,  # noqa: spanish — Damian's quoted remark
    miro el CoT, se me ocurre otra cosa y te interrumpo»): front-runs    # noqa: spanish — continuation
    queued item `idx`, stops the running turn COOPERATIVELY (session/stop
    with the worker's own runtime — it SURVIVES warm) and flags the pickup
    to run the message with the redirect preamble. The queue is NOT
    cleared: a plain /stop keeps its F17 discard semantics — this is a
    redirect, not a cancel. The stop marker itself is consumed by the
    turn's own finally (per-turn cleanup), so the warm wait never sees
    it."""
    ent = _workers().get(sid)
    t = title or (ent or {}).get("title") or sid[-8:]
    moved = [None]

    def front(q):
        q = q if isinstance(q, dict) else {}
        lst = list(q.get(sid) or [])
        if not (0 <= idx < len(lst)):
            return None
        moved[0] = lst[idx]
        q[sid] = [lst[idx]] + lst[:idx] + lst[idx + 1:]
        return q
    try:
        _store(config.QUEUE_FILE).update_cross_process(front, {})
    except OSError as e:
        pwlog("F58 interrupt queue_mutate FAILED: %s" % e)
        return ("⚠️ The queue is stuck — I interrupted nothing, retry in a "
                "moment.")
    if moved[0] is None:
        return "🤔 That message is no longer in the queue."
    if ent is None:
        if sid in running_turns():
            # a Mac-side turn: the phone cannot stop it (the app's ⏹ does)
            # — but the message is FIRST for whoever drains the queue
            return ("🖥️ «%s» is a Mac-side turn — I can't interrupt it "
                    "from here (the app's ⏹ does). Your message moved to "
                    "the FRONT: it runs first when it ends." % esc(t))
        return ("✅ Nothing is running anymore — your message is first in "
                "the queue; it starts on its own in a moment.")
    _interrupt_request(sid)   # the pickup runs it WITH the preamble
    _stop_request(sid)        # the worker sees it in ~2 s → session/stop
    pwlog("F58 interrupt: %s item %d front-run (cooperative stop)"
          % (sid[-8:], idx))
    return ("⏹️ Interrupting «%s» — the turn stops in a few seconds and "
            "your message takes over the SAME runtime (context kept, "
            "warm start)." % esc(t))


def _twin_q(sid_eff, fingerprint):
    """rid of the IDENTICAL question already pending in the channel (same
    sid and same questions → same fingerprint): the CLI's re-fire reuses
    it without re-showing the buttons. None if there is no twin."""
    for q in _questions().pendings():
        if q.get("sid") == sid_eff and _perm_key(
                sid_eff, "AskUserQuestion",
                {"questions": q.get("questions") or []}) == fingerprint:
            return q.get("request_id")
    return None


def _question_msg(qs, title):
    """R10 (pure) — (text, keyboard) of the ❓ sent to the chat: a single
    question maps buttons 1:1 to its options; a questionnaire lists
    everything without buttons (the answer comes by number). Testable
    without mocks."""
    lines = ["❓ <b>%s</b>" % esc(title)]
    kb = []
    if len(qs) == 1:
        # a single question: the buttons map 1:1 to the options
        # used by number resolution (handle_free_text uses qs[0])
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
    lines.append("\nReply with the number (or tap a button)." if kb
                 else "\nReply with the number (or free text).")
    return "\n".join(lines), ({"inline_keyboard": kb} if kb else None)


def _user_input_handler(cfg, sid, title):
    """Callback for interaction/requestUserInput: sends the ❓ to the chat
    and waits for the answer through a FILE (F16: the poller writes it on
    button/number press — there is no shared memory between processes).
    F15: the question carries ITS OWN sid — with several tasks the answer
    routes to the one that asked (handle_free_text prioritizes the
    current task)."""
    def handler(params):
        qs = params.get("questions") or []
        sid_eff = sid or (active_bot_session().get("sid"))
        # FIX 14/10 v2 (questionnaires in a loop): the CLI RE-FIRES the same
        # AskUserQuestion and the handler re-created the entry and re-showed
        # the buttons — Damian picked an option and saw the question again.
        # (The F27 replay only covered the poller's hook, not this path.)
        # Layers: (1) answered <10 min ago → return directly; (2) identical
        # one pending → reuse its rid WITHOUT re-showing; (3) new → show.
        fingerprint = _perm_key(sid_eff, "AskUserQuestion", {"questions": qs})
        previous = _perm_replay(fingerprint)
        if previous is not None:
            return {"action": "accept", "content": {"answer": previous}}
        rid = _twin_q(sid_eff, fingerprint)
        if rid is None:
            rid = str(params.get("requestId") or time.time())
            # /task new: the real sid is born after the spawn — for that
            # window the one the worker sets as the active session applies
            _questions().ask(rid, qs, title, sid_eff)
            q_text, markup = _question_msg(qs, title)
            uhttp.send(cfg, q_text, loud=True, markup=markup)
        resp = _questions().wait(rid, timeout=480)
        if resp is not None:
            _perm_remember(fingerprint, resp)  # the re-fire doesn't ask again
            # F36: "✅ Respondido" (from the poller) says it reached the BOT;
            # the actual delivery to the agent is THIS line — component ack
            acks.choice_received(
                cfg, "answer to the agent: %s" % preview(str(resp), 60),
                "the turn continues with your choice")
            return {"action": "accept",
                    "content": {"answer": resp}}
        uhttp.send(cfg, "⌛️ The question from «%s» expired with no answer." % esc(title))
        return {"action": "decline", "reason": "timeout telegram"}
    return handler


def _queue_put(sid, act, text, cfg=None):
    """True if it queued. (20/10 audit: the flock OSError was swallowed
    and callers told the owner "it stayed in the queue" — a lie: the
    message was lost without a trace.) F51b: with a full queue (5) the
    oldest one was dropped IN SILENCE — if there is a cfg we report
    which one fell."""
    if isinstance(act, BotSession):
        act = act.to_dict()  # the queue persists dicts (QueueItem in F4g)
    dropped = [None]

    def add(q):
        q = q if isinstance(q, dict) else {}
        lst = q.get(sid) or []
        whole = lst + [QueueItem(act, text[:6000], time.time()).to_dict()]
        if len(whole) > 5:
            dropped[0] = (whole[0].get("text") or "")[:60]
        q[sid] = whole[-5:]
        return q
    try:
        # F16: flock — the poller and worker processes write the same queue
        _store(config.QUEUE_FILE).update_cross_process(add, {})
        if dropped[0] is not None and cfg is not None:
            uhttp.send(cfg, "⚠️ Queue full (5) — dropped the oldest one: "
                            "«%s»" % dropped[0], loud=False)
        return True
    except OSError as e:
        pwlog("queue_put FAILED (%s): the enqueue is lost" % e)
        return False


def _queue_take(sid):
    taken = [[]]

    def take(q):
        q = q if isinstance(q, dict) else {}
        taken[0] = q.pop(sid, [])
        return q
    try:
        _store(config.QUEUE_FILE).update_cross_process(take, {})
    except OSError as e:
        # the take didn't apply: the queue REMAINS in the file — launch
        # nothing (launching without taking would duplicate the item)
        pwlog("queue_take FAILED (%s): queue intact, not launching" % e)
        taken[0] = []
    return [QueueItem.from_dict(e) for e in taken[0]]


def _queue_drain(cfg, sid):
    """When a turn ends, if prompts remain queued for the session, launch
    the next one (chain). F17: if the session is STILL busy (a Mac run),
    the queue stays ASLEEP — without this guard the relay re-spawned in
    a loop while the task was busy (incident 18:03: 5 workers in 5 s,
    each with its own runtime). F15: if it is no longer the current
    task, it also sleeps (/task resumes it)."""
    if not sid:
        return
    if _nuclear_active():
        pwlog("drain skip: nuclear in progress (queue idle: %d)"
              % len(_queue_peek(sid)))
        return
    if sid in running_turns():
        pwlog("drain skip: %s still busy (queue idle: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    if not _task_current_ok(cfg, sid):
        pwlog("drain skip: %s is not the current task (queue idle: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    _queue_launch_next(cfg, sid)


def _requeue(sid, items):
    """R0c — return items to the queue PRESERVING FIFO order
    (_queue_put appends). The latent bug: _warm_wait re-queued with
    reversed() + append, which INVERTED the queue when ≥2 items remained
    (the user's messages would have run in reverse);
    _queue_launch_next already did it forward — one body for the three
    places that return items."""
    for it in items:
        _queue_put(sid, it["act"], it["text"])


def _subscribe_seq(c, sid):
    """F9 — subscribe + stream cursor (eventSeq) to wait for the end of
    the turn BY EVENTS. If it fails, None: _turn_end_poll degrades to
    scanning the CLI logs (the usual path). R3: the three copies of the
    ritual (turn, rotation summary, new session seed) live here."""
    try:
        r = c.request("session/subscribe",
                      {"sessionId": sid,
                       "deliveryKind": "desktop-continuous"}, timeout=15)
        return ((r or {}).get("result") or {}).get("eventSeq")
    except Exception:  # noqa: BLE001
        log_exc("errors", "session/subscribe failed (seq=None — "
                          "degrading to log scan)")
        return None


# R3: the retry keyboard for the error paths — used to be THREE literals
_RETRY_KEYBOARD = {"inline_keyboard": [[
    {"text": "🔁 Retry", "callback_data": "retry"}]]}

# F58: what the redirected prompt travels with — the model must know it
# was interrupted mid-flight (the cut in its history is real) and that
# this message REPLACES the plan, not continues it
_INTERRUPT_PREAMBLE = (
    "[redirect] You were interrupted mid-turn by the user. The partial "
    "work already in this conversation stands as context — reassess it "
    "and follow the NEW instruction below instead of the old plan.\n\n")


def _current_sel(snap_sel):
    """F7++ (pure) — the turn snapshot's modelSelection, which travels as
    a JSON string through the store (15/10 lesson: comparing a string
    against a dict was ALWAYS False) or as a dict. None if it can't be
    read: no skip, it gets restored."""
    if isinstance(snap_sel, str):
        try:
            return json.loads(snap_sel).get("modelSelection")
        except ValueError:
            return None
    if isinstance(snap_sel, dict):
        return snap_sel.get("modelSelection", snap_sel)
    return None


def _queue_launch_next(cfg, sid):
    """Takes the session's next queued prompt and launches its worker
    (F16: detached process — the queue chain survives poller
    restarts). Returns (item, remaining) or None if there was nothing."""
    lst = _queue_take(sid)
    if not lst:
        return None
    item = lst[0]
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        # (20/10 audit) the spawn failed: return EVERYTHING in order —
        # putting the item back at the end INVERTED the FIFO ([A,B,C] became [B,C,A])
        _requeue(sid, lst)
        return None
    _requeue(sid, lst[1:])  # we return them to the queue (FIFO order)
    return item, len(lst) - 1


def _queue_resume(cfg, sid, title=None):
    """F15 — auto-resume: when a task (via /task) that had queued prompts
    comes back, start the first one. If that session already has a live
    worker, the queue continues on its own when its turn ends."""
    if not sid:
        return None
    if _nuclear_active():
        pwlog("resume skip: nuclear in progress")
        return None
    if _workers().get(sid) is not None:
        n = len(_queue_peek(sid))
        if n:
            return ("⏳ A turn is already running in «%s» — your %d queued "
                    "run when it finishes." % (esc(title or sid[-8:]), n))
        return None
    res = _queue_launch_next(cfg, sid)
    if not res:
        return None
    remaining = res[1]
    return ("⏳ Resumed the queue of «%s»%s — working…"
            % (esc(title or sid[-8:]),
               " (%d more queued)" % remaining if remaining else ""))


_drain_ts = [0.0]  # own throttle: the poller calls every cycle, the
                   # real check runs at most every `every_s` seconds


def queue_check(cfg: Dict[str, Any], every_s: float = 20.0) -> None:
    """F37a — autonomous drain: wakes dormant queues once the session is
    free, WITHOUT depending on anyone else to notice. Covers the gap
    from the 19/10 15:47 incident: the F17 queue waited for the on_stop
    of a Mac turn the app NEVER launched (prompt swallowed while the
    task was busy) — the message slept 88 minutes until the manual
    /task. Same guards as _queue_drain: nuclear, live worker, busy
    session and current task (F15: the others sleep until their /task)."""
    now = time.time()
    if now - _drain_ts[0] < every_s:
        return
    _drain_ts[0] = now
    if _nuclear_active():
        return
    for sid in _queue_sids_with_items():
        if _workers().get(sid) is not None or sid in running_turns() \
                or not _task_current_ok(cfg, sid):
            continue
        res = _queue_resume(cfg, sid)
        if res:
            uhttp.send(cfg, res, loud=False)


# -------------------------------------------- F8a: warm worker (ADR-014)

def _queue_rekey(old, new):
    """F8b: move one session's queued prompts to another (rotation).
    Mirror of the registry's rekey; atomic via flock."""
    if not old or not new or old == new:
        return

    def fn(q):
        q = dict(q) if isinstance(q, dict) else {}
        lst = q.pop(old, [])
        if not lst:
            return None  # nothing to move: don't write
        q[new] = (q.get(new) or []) + lst
        return q
    try:
        _store(config.QUEUE_FILE).update_cross_process(fn, {})
    except OSError:
        pass  # silent-by-design: rotation bookkeeping, the next put/take self-heals


def _warm_wait(cfg, sid, timeout_s):
    """The warm worker waits for ITS session's next prompt with the
    connection already resumed (the ~30s resume was paid once:
    back-to-back messages start instantly). Returns (text, act) of the
    item taken or None (timeout / /stop marker / the Mac took over the
    session). ~1s loop reading small files — no runtime, no CPU."""
    t0 = time.time()
    t_hb = 0.0
    while time.time() - t0 < timeout_s:
        if _stop_requested(sid):
            _stop_consume(sid)
            pwlog("warm: /stop marker — exiting warm")
            return None
        if time.time() - t_hb >= 60:
            try:
                _workers().heartbeat(sid)
            except Exception:  # noqa: BLE001
                log_exc("errors", "warm heartbeat write failed (%s)"
                        % sid[-8:])
            t_hb = time.time()
        if _queue_peek(sid):
            lst = _queue_take(sid)
            if lst:
                if sid in running_turns():
                    # F17: the Mac took the session while we waited —
                    # return EVERYTHING and exit (the cold path picks it up)
                    _requeue(sid, lst)
                    pwlog("warm: task busy — re-queuing (%d) and exiting"
                          % len(lst))
                    return None
                _requeue(sid, lst[1:])
                pwlog("warm: pickup (%d left in queue)" % (len(lst) - 1))
                return lst[0]["text"], lst[0]["act"]
        if config.code_changed():
            # F48d: there was a deploy — idle with old code in memory, I
            # step aside so the next message starts cold with the new one
            # (what was queued was already served above: nothing left hanging)
            pwlog("warm: new code deployed — exiting so the next "
                  "turn starts cold")
            return None
        time.sleep(_WARM_STEP_S)
    pwlog("warm: keepalive expired (%.0fs) — runtime released" % timeout_s)
    return None


# -------------------------------------------- F8b: session rotation

_SUMMARY_PROMPT = (
    "I'm going to migrate this conversation to a new session due to "
    "size. Write the continuity summary: current goal, decisions made, "
    "state of the code, open tasks and next steps. Only the summary, "
    "no preamble or farewells.")


def _rotation_deferred(cfg, sid, title, phase):
    """F28 — a message arrived mid-dance: abort CLEANLY. The checks live
    ONLY between phases and before touching anything persistent
    (bot-active, registry and queue stay on the old session) → continuity
    intact; the caller falls into _warm_wait, serves the message on the
    current session and the rotation is retried by the next warm keepalive
    (the gate condition — empty queue + transcript over threshold —
    becomes True again)."""
    pwlog("rotation deferred after %s (sid %s)" % (phase, sid[-8:]))
    uhttp.send(cfg, "♻️ «%s» — one of your messages arrived mid-rotation: "
               "I'm leaving it here (the current session stays perfectly "
               "fine), I'll handle it and resume the rotation once it's "
               "free."
               % esc(title))
    return None


def _transcript_mb(sid):
    """MB of the transcript (rollout model-io jsonl) — the implementation
    lives in core (shared with _proto_ready for the timeouts)."""
    return transcript_mb(sid)


def _rotate_session(c, cfg, act, sid, ws, title, model, old_snap):
    """F8b: the dance — compact + summary turn + new session seeded with
    that summary (the official pattern for heavy resumes: short sessions
    + seeded memory). Returns (c_nueva, sid_nuevo, act_nuevo) or None if
    anything failed: it reports and STAYS on the old one — continuity is
    never lost (the old session stays archived, reachable via /task).
    Interruptible (F28): message in the queue between phases → aborts
    cleanly via _rotation_deferred, the warm worker serves the message
    on the old one."""
    mb = _transcript_mb(sid)
    pwlog("rotation: transcript %.0fMB > threshold — compacting" % mb)
    uhttp.send(cfg, "♻️ «%s» accumulated %.0fMB of transcript — rotating "
               "to a fresh session (compact + summary)…" % (esc(title), mb))
    if _stop_requested(sid):
        return None
    try:
        r = c.request("session/compact", {"sessionId": sid},
                      timeout=_compact_timeout(sid))
    except Exception as e:  # noqa: BLE001
        log_exc("errors", "compact failed (staying on the session)")
        uhttp.send(cfg, "🤔 I couldn't compact (%s) — staying on the "
                   "current session." % esc(str(e)[:100]))
        return None
    if e := zc_proto.proto_err(r, "compact"):
        uhttp.send(cfg, "🤔 The compact was rejected — staying on the "
                   "current session: %s" % esc(e[:120]))
        return None
    end = _wait_cli_event(sid, time.time(),
                          ("compact.completed", "compact.failed"),
                          _compact_timeout(sid) + 30)
    if end is None or end.get("event") == "compact.failed":
        uhttp.send(cfg, "🤔 The compact didn't finish well — staying on "
                   "the current session.")
        return None
    if _stop_requested(sid):
        return None
    if _queue_peek(sid):
        return _rotation_deferred(cfg, sid, title, "compact")
    # summary turn: the compacted session already has a small context
    pre = _fetch_messages(c, sid, limit=1)
    last_id = _last_id_of(pre)
    # F9: stream cursor for the event-based end (degrades to logs if it fails)
    seq_res = _subscribe_seq(c, sid)
    try:
        c.request("session/send",
                  {"sessionId": sid,
                   "content": _headless_text(_SUMMARY_PROMPT, cfg),
                   "modelSelection": model or MODEL_SEL}, timeout=120)
    except Exception as e:  # noqa: BLE001
        log_exc("errors", "summary send failed (staying on the session)")
        uhttp.send(cfg, "🤔 The summary turn couldn't be sent (%s) — "
                   "staying on the current session." % esc(str(e)[:100]))
        return None
    _turn_end_poll(sid, time.time(), timeout=300,
                   stop_check=lambda: _stop_requested(sid),
                   t_send=time.time(), conn=c, start_seq=seq_res)
    msgs = _fetch_messages(c, sid, after_id=last_id, limit=5) or []
    summary = _extract_assistant_text(msgs, last_only=True).strip()[:4000]
    if not summary:
        uhttp.send(cfg, "🤔 I couldn't get the summary — staying on the "
                   "current session.")
        return None
    if _stop_requested(sid):
        return None
    if _queue_peek(sid):
        return _rotation_deferred(cfg, sid, title, "summary")
    # new session on its own runtime (the old one's giant resume is never
    # paid again: the new transcript starts at ~KB)
    holder = {"sid": None}

    def on_input(params):
        _user_input_handler(cfg, holder["sid"], title)(params)

    def on_perm(params):
        pwlog("🔐 headless permission: %s"
              % json.dumps(params, ensure_ascii=False)[:200])
        uhttp.send(cfg, "🔐 «%s» asked for a headless permission (it "
                   "should run yolo — let me know to review it): "
                   "<code>%s</code>"
                   % (esc(title), esc(json.dumps(
                       params, ensure_ascii=False)[:120])))
    try:
        c2 = zc_proto.make(cwd=ws, on_user_input=on_input,
                           on_permission=on_perm)
    except OSError as e:
        uhttp.send(cfg, "🤔 I couldn't launch the new runtime (%s) — "
                   "staying on the current session." % esc(str(e)[:100]))
        return None
    sid2, err = _create_session(c2, ws)
    if err:
        try:
            c2.close()
        except Exception:  # noqa: BLE001
            pass  # silent-by-design: closing a failed runtime, nothing left to do
        uhttp.send(cfg, "🤔 I couldn't create the new session — staying "
                   "on the current one: %s" % esc(err[:120]))
        return None
    holder["sid"] = sid2
    seed = ("You are the continuation of a previous session of this "
            "project («%s»), rotated due to size. Inherited state:\n\n"
            "%s\n\nPick up from there on the next message; reply to this "
            "one with just a brief OK." % (title, summary))
    r = c2.request("session/send",
                   {"sessionId": sid2,
                    "content": _headless_text(seed, cfg),
                    "modelSelection": model or MODEL_SEL}, timeout=120)
    if e := zc_proto.proto_err(r, "send"):
        try:
            c2.close()
        except Exception:  # noqa: BLE001
            pass  # silent-by-design: closing a failed runtime, nothing left to do
        uhttp.send(cfg, "🤔 The new session didn't accept the seed — "
                   "staying on the current one: %s" % esc(e[:120]))
        return None
    seq_seed = _subscribe_seq(c2, sid2)
    _turn_end_poll(sid2, time.time(), timeout=180,
                   stop_check=lambda: _stop_requested(sid),
                   t_send=time.time(), conn=c2, start_seq=seq_seed)
    act2 = dict(act, sid=sid2)
    try:
        set_active_bot_session({**active_bot_session(), **act2})
    except Exception:  # noqa: BLE001
        log_exc("errors", "rotation: active-session update failed")
    try:
        _workers().rekey(sid, sid2)  # the /stop button points to the new one
    except Exception:  # noqa: BLE001
        log_exc("errors", "rotation: worker rekey failed")
    _queue_rekey(sid, sid2)
    _restore_model(sid, old_snap)  # the old one stays clean (archived)
    uhttp.send(cfg, "♻️ «%s» rotated: %.0fMB of transcript → summary "
               "seeded. The chat continues in the new session <code>%s</"
               "code> (the old one stayed archived) — I'm waiting for "
               "your next message, just write me."
               % (esc(title), mb, esc(sid2[-8:])))
    pwlog("rotation OK: %s -> %s (summary %d chars)"
          % (sid, sid2, len(summary)))
    return c2, sid2, act2


def _active_turn_register(sid, title, t0, ws=None, last_id=None):
    """Persistent record of the in-flight turn: if the worker dies WITHOUT
    running its finally (kill -9 / OOM), the poller's supervisor sees it
    and delivers the ending — nobody dies in silence."""
    if not sid:
        return

    def add(q):
        q = q if isinstance(q, dict) else {}
        q[sid] = {"title": title, "t0": t0, "pid": os.getpid(),
                  "ws": ws, "last_id": last_id}
        return q
    try:
        _store("bot-active.json").update_cross_process(add, {})
    except OSError as e:
        pwlog("active_turn_register FAILED (%s): a hard kill would go "
              "unreported" % e)


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
        pass  # silent-by-design: cleanup, a stale entry is reaped by orphans_check


def _active_turn_mark(sid, **fields):
    """Writes fields into the in-flight turn's record IF it exists (does
    not create it): /stop leaves stop=<epoch> so that the orphan
    supervisor, if the worker dies finished off, writes «frenada» and not
    «falló» (F47c)."""  # noqa: spanish — quoted old banner
    if not sid:
        return

    def upd(q):
        q = q if isinstance(q, dict) else {}
        ent = q.get(sid)
        if isinstance(ent, dict):
            ent.update(fields)
        return q
    try:
        _store("bot-active.json").update_cross_process(upd, {})
    except OSError as e:
        pwlog("active_turn_mark FAILED (%s): the orphan report may misread "
              "the death" % e)


def _spawn_worker(cfg, act, text):
    """F16: launches the worker as a detached PROCESS (start_new_session=True,
    the stuck-watch pattern). A poller kickstart/restart/crash does NOT
    touch it: the turn lives its full cycle and reports to Telegram on
    its own. JSON payload via stdin (argv falls short for 6KB prompts)."""
    if _nuclear_active():
        # Nuclear 2.0 — lockdown: NOTHING new starts during the cycle
        # (before, the poller could spawn a worker mid-nuclear that died
        # with the kickstart without a report). The queue goes dormant
        # and survives: the auto-resume drains it when it comes back.
        pwlog("spawn blocked: nuclear in progress")
        return None
    _save_last_prompt(act, text)  # F22: /retry always has something to relaunch
    if isinstance(act, BotSession):
        act = act.to_dict()  # the stdin mailbox travels as a flat dict
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")  # shim: entrypoint for subprocesses
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        with open(os.path.join(config.LOG_DIR, "worker-stderr.log"), "a") as err:
            p = subprocess.Popen(
                [sys.executable, me, "--run-turn"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                start_new_session=True,
                cwd=act.get("ws") or os.path.expanduser("~"))
            # F30: orphans_check reaps it when it dies — BEFORE the write:
            # a BrokenPipe (shim dead at startup) left it out of the
            # list = a permanent poller zombie (20/10 audit)
            _SPAWNED.append(p)
            p.stdin.write(json.dumps({"act": act, "text": text}).encode("utf-8"))
            p.stdin.close()
        return p
    except OSError as e:
        audit.record((act or {}).get("sid"), "spawn",
                     "couldn't launch the worker: %s" % str(e)[:120])
        uhttp.send(cfg, "❌ I couldn't launch the worker: " + esc(str(e)[:120]))
        return None


# F30: Popens of THIS process's workers. The poller never called wait() →
# every dead worker stayed <defunct> forever (13066 from the 18:10
# incident). orphans_check calls _reap_children every cycle: Popen.poll is
# waitpid WNOHANG — reclaims the dead, keeps the living, zero blocking.
_SPAWNED: List[subprocess.Popen] = []


def _reap_children():
    alive = []
    for p in _SPAWNED:
        try:
            if p.poll() is None:
                alive.append(p)
        except Exception:  # noqa: BLE001 — the reap never breaks the supervisor
            pass  # silent-by-design: a broken Popen handle is dropped, that IS the reap
    _SPAWNED[:] = alive


def cmd_run_turn() -> None:
    """F16: entrypoint of the worker process (--run-turn). Reads {act, text}
    from stdin, installs SIGTERM → marker (gentle nuclear: the shutdown
    goes through the same path as /stop) and a safety atexit for the
    registry; then runs the same prompt_worker as always."""
    import atexit
    import signal
    from zcode_bot.utils.logging import bind_context
    bind_context(component="worker")
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
        pass  # silent-by-design: no signal handling available, /stop still works
    atexit.register(lambda: _workers().pop_if_mine(sid or "nueva"))
    prompt_worker(load_config(), act, text)


def _run_turn(c, cfg, act, text, sid, ws, title, model, stop_hook,
              ph=None):
    """F8a: ONE full turn over an ALREADY active session (cold or warm —
    same connection, same protocol; the only difference is whether ph is
    born here or comes from the cold start). Delivers via Telegram and
    returns: 'ok' | 'error' (the warm worker can continue) | 'stop'.
    R0b: 'abandoned' NO LONGER exists — while the turn lives we wait
    (notice every 30 min); the only cutoff is /stop. The worker's
    lifecycle (registry/bot-active/marker/runtime/queue) stays outside,
    in prompt_worker."""
    import threading as _th
    if ph is None and cfg.get("live_progress"):
        # WARM turn: the box still arrives, but instantly
        ph = uhttp._live_send(cfg, "🧠 <b>«%s»</b> received — running…"
                              % esc(title))
    try:
        if _stop_requested(sid):
            # /stop arrived before sending: don't even send the turn
            uhttp.send(cfg, "⏹️ «%s» stopped by /stop (before sending)." % esc(title))
            if ph:
                uhttp._live_edit(cfg, ph, "⏹️ <b>«%s»</b> stopped by /stop"
                                 % esc(title))
            return "stop"
        pre = _fetch_messages(c, sid, limit=1)
        last_id = _last_id_of(pre)
        t0 = time.time()
        # F9: subscribe BEFORE the send — the cursor (eventSeq) comes from
        # here and guarantees the turn's turn.started/completed ENTER the
        # stream (with incremental afterSeq, frames in KB). If it fails,
        # start_seq=None and _turn_end_poll degrades to scanning logs.
        start_seq = _subscribe_seq(c, sid)
        # ADR-020: the brain's memories ride in front of the prompt; the bus
        # keeps the ORIGINAL text so extraction never feeds on its own output
        mem_block, mem_ids = brain_client.memory_context(cfg, text)
        if mem_ids:
            pwlog("memory_inject: %d item(s) %s, %d chars" % (len(mem_ids), mem_ids, len(mem_block)))
        r = c.request("session/send",
                      {"sessionId": sid, "content": _headless_text(mem_block + text, cfg),
                       "modelSelection": model or MODEL_SEL}, timeout=120)
        pwlog("send resp %.1fs:" % (time.time() - t0), json.dumps(r)[:200])
        if e := zc_proto.proto_err(r, "send"):
            audit.record(sid, "send", e[:150])  # F30: correlation
            uhttp.send(cfg, e)
            if ph:
                uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> failed: %s"
                                 % (esc(title), e))
            return "error"
        t_send = time.time()  # F20: anchor for OUR OWN turnId (crossed turn)
        _active_turn_register(sid, title, t0, ws=ws, last_id=last_id)
        turn = bus.turn_id(t0)  # ADR-019: one id for every tap of this turn
        bus.tap(cfg, "turn.started", sid, turn=turn, payload={
            "title": title, "model": model or MODEL_SEL, "ws": ws,
            "prompt_chars": len(text), "prompt": bus.clip(text), "memory_ids": mem_ids})

        _notice_at = [0]  # R0b: the thread and the direct poll share the turn

        def long_turn_notice(minutes):
            # R0b: while the turn LIVES, here we stay — the 40-min
            # abandonment killed the runtime with it ("Turn was cancelled"
            # at the same second the worker exited, 20/10 14:55). The cell
            # deduplicates: with a live view there are TWO waiting (the
            # thread and the backup poll) and last night the 60-min notice
            # came twice, with a 35 glued to the 30 from the catch-up of
            # the one that starts late.
            if minutes < _notice_at[0] + 25:
                return
            _notice_at[0] = minutes
            if not _task_current_ok(cfg, sid):
                return  # F72: a progress notice never lands over the task
                # the user switched to (the final reply still arrives)
            pwlog("long turn: %d min — still waiting" % minutes)
            uhttp.send(cfg, "⏱️ «%s» has been running for %d min — I'm "
                      "still with it (/stop stops it)."
                      % (esc(title), minutes), loud=False)

        wait_kw = dict(timeout=None, stop_check=stop_hook, t_send=t_send,
                       conn=c, start_seq=start_seq, notice=long_turn_notice)
        if ph:
            res = {}
            th = _th.Thread(target=lambda: res.update(
                end=_turn_end_poll(sid, t0, **wait_kw)),
                daemon=True)
            th.start()
            lr = LiveTurnView(c, cfg, sid, t0, title, ph, th,
                              last_id=last_id, start_seq=start_seq).run()
            live_cot = (lr[0] if lr else "") or ""
            live_box = (lr[3] if lr else None) or ph  # live box (below if there was CoT)
            end = res.get("end")
            if end is None:  # the view cut at ITS own cap: the turn continues — keep waiting
                end = _turn_end_poll(sid, t0, **wait_kw)
        else:
            end = _turn_end_poll(sid, t0, **wait_kw)
            live_cot = ""
            live_box = ph
        pwlog("end:", (end or {}).get("event"), "rss=%dMB rss_now=%dMB"
              % (_rss_mb(), _rss_now_mb()))
        if ph:
            # R0b: end is NEVER None here — the wait has no cap
            if end.get("event") == "turn.failed":
                if _interrupt_pending(sid):
                    # F58: this «failure» is the interrupt LANDING
                    # (session/stop cancels as turn.failed «Turn was
                    # cancelled») — not an error, the redirect is next
                    uhttp._live_edit(cfg, live_box,
                                     "⏹️ <b>«%s»</b> interrupted — your "
                                     "message is next…" % esc(title))
                else:
                    uhttp._live_edit(cfg, live_box,
                                     "❌ <b>«%s»</b> failed — detail below"
                                     % esc(title))
            else:
                uhttp._live_edit(cfg, live_box, "✅ <b>«%s»</b> · reply:" % esc(title))
        if end.get("event") == "turn.failed":
            if _interrupt_pending(sid):
                # F58: same as above — the ⏹️ button did this; the warm
                # pickup runs the redirect in seconds (no retry button:
                # nothing failed)
                bus.tap(cfg, "turn.failed", sid, turn=turn, payload={
                    "error": "interrupted", "duration_s": round(time.time() - t0, 1)})
                uhttp.send(cfg, "⏹️ «%s» interrupted — your message takes "
                           "over the same runtime…" % esc(title))
                return "error"
            err = str((end.get("error") or {}).get("message", ""))[:150]
            audit.record(sid, "turn-failed", err)  # F30: correlation
            bus.tap(cfg, "turn.failed", sid, turn=turn, payload={
                "error": err, "duration_s": round(time.time() - t0, 1)})
            uhttp.send(cfg, "❌ «%s» failed: %s" % (esc(title), esc(err)),
                 markup=_RETRY_KEYBOARD)
            return "error"
        if _workers().stopped(sid):
            bus.tap(cfg, "turn.failed", sid, turn=turn, payload={
                "error": "stopped", "duration_s": round(time.time() - t0, 1)})
            if ph:
                uhttp._live_edit(cfg, live_box, "⏹️ <b>«%s»</b> stopped" % esc(title))
            uhttp.send(cfg, "⏹️ «%s» stopped by /stop." % esc(title))
            return "stop"
        dur = (time.time() - t0) / 60.0
        msgs = _fetch_messages(c, sid, after_id=last_id, limit=120) or []
        # F20: ONLY the last assistant message — in a session shared with
        # the app there are foreign answers in the window (incident 18:29)
        reply_text = _extract_assistant_text(msgs, last_only=True).strip()
        bus.tap(cfg, "turn.completed", sid, turn=turn, payload={
            "duration_s": round(time.time() - t0, 1), "reply_chars": len(reply_text),
            "reply": bus.clip(reply_text), "cot_chars": len(live_cot)})
        _turn_trace(cfg, title, sid, dur, msgs, live_cot=live_cot)
        if not reply_text:
            uhttp.send(cfg, "✅ «%s» done in %.0f min (no reply text)." % (esc(title), dur))
            return "ok"
        _send_long_reply(cfg, title, dur, reply_text)
        return "ok"
    except Exception as e:
        # a dead worker is NEVER silent anymore: notify the chat + log
        import traceback
        pwlog("worker CRASH:", traceback.format_exc())
        audit.record(sid, "crash", str(e)[:150])  # F30: correlation
        try:
            uhttp.send(cfg, "⚠️ The worker of «%s» died processing your "
                  "message: <code>%s</code>" %
                  (esc(title), esc(str(e)[:150])), loud=True,
                  markup=_RETRY_KEYBOARD)
        except Exception:
            log_exc("errors", "crash report send ALSO failed (chat blind?)")
        return "error"
    finally:
        # PER-TURN cleanup (the warm worker may run the next one): the
        # active entry and the marker must not outlive the turn
        _active_turn_clear(sid)
        _stop_consume(sid)


def prompt_worker(cfg: Dict[str, Any],
                  act: Union[BotSession, Dict[str, Any]], text: str) -> None:
    """Runs a headless prompt on the active conversation and pushes the
    answer to Telegram. Lives in a PROCESS (F16: actor). F8a: if
    warm_keepalive_s > 0, after the turn it stays WARM — same runtime,
    same sid, waiting for the session's next prompt (_warm_wait) —
    and rotates the session if the transcript grew too much (F8b)."""
    sid, ws, title = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    key = sid or "nueva"
    if sid and _workers().get(sid) is not None:
        # a worker already exists for the session (poller/queue_drain race):
        # queue without notifying — the running one drains when it finishes
        _queue_put(sid, act, text)
        return
    if sid and sid in running_turns():
        # F17: the task is busy (a Mac run, none of our workers in it)
        # → DORMANT queue, no runtime spawned (the relay looping burned
        # ~500MB per cycle). Whoever finishes wakes it: the Mac's on_stop
        # or the worker's drain.
        pwlog("busy -> queue (task busy, no runtime)")
        if not _queue_put(sid, act, text, cfg=cfg):
            uhttp.send(cfg, "⚠️ The Mac is busy and I could NOT queue your "
                      "message — retry in a moment.")
            return
        uhttp.send(cfg, "⏳ The Mac is busy with «%s» — your message stayed "
                  "in the queue (%d waiting). It runs when the run "
                  "finishes (or /stop to clear it; /nuclear in an "
                  "emergency)."
                  % (esc(title), len(_queue_peek(sid))))
        return
    if not _workers().register(key, WorkerEntry(
            t0=time.time(), prompt=text[:80], ws=ws, title=title,
            pid=os.getpid()).to_dict()):
        # (20/10 audit) ANOTHER instance won the race between the get
        # above and this register (queue_check wakes the queue and the
        # user's message arrives within the other process's ~0.5s of
        # bootstrap): without this check TWO workers ran with crossed
        # turns over the same session (the class of the F20 incident).
        pwlog("register lost the race (%s) -> queue" % key)
        if sid:
            _queue_put(sid, act, text)
        return
    pwlog("worker start sid=%s pid=%d title=%s rss=%dMB text=%r"
          % (sid, os.getpid(), title, _rss_mb(), text[:60]))
    # F7+: the placeholder arrives in ~2s (before touching the runtime) —
    # the full startup (resuming a large session) can take tens of
    # seconds and the chat must not go silent while waiting
    ph = None
    if cfg.get("live_progress"):
        ph = uhttp._live_send(cfg, "🧠 <b>«%s»</b> starting…" % esc(title))
    t_arr = time.time()
    try:
        c = zc_proto.make(cwd=ws,
                          on_user_input=_user_input_handler(cfg, sid, title),
                          on_permission=lambda params: (
                               pwlog("🔐 headless permission: %s"
                                     % json.dumps(params, ensure_ascii=False)[:200]),
                               uhttp.send(cfg, "🔐 «%s» asked for a headless "
                                         "permission (it should run yolo — "
                                         "let me know to review it): "
                                         "<code>%s</code>"
                                         % (esc(title), esc(json.dumps(
                                             params, ensure_ascii=False)[:120])))))
        if ph:
            # F7+++: starting a large session takes tens of seconds —
            # the box should show WHAT it's doing, not "starting"
            uhttp._live_edit(cfg, ph,
                             "🧠 <b>«%s»</b> opening the session (large "
                             "history: can take a minute)…" % esc(title))
    except OSError as e:
        _workers().pop(key)  # CAREFUL, key: pop(None) left the "nueva" orphan (bug F0)
        audit.record(sid, "runtime", str(e)[:120])  # F30: correlation
        uhttp.send(cfg, "❌ I couldn't launch the headless runtime: " + esc(str(e)[:120]))
        if ph:
            uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> couldn't launch the runtime"
                             % esc(title))
        return
    # F16: /stop goes through a marker (state/bot-stop/<sid>) — the
    # ProtoClient is no longer shared with the poller (pipes don't cross
    # processes)
    snap_sel = None  # /stop may strike BEFORE the assignment (the finally reads it)
    stop_done: List[str] = []

    def stop_hook():
        """F16: _turn_end_poll's hook — if the poller left a /stop marker,
        stop with the worker's OWN runtime (a real stop)."""
        if stop_done or not _stop_requested(sid):
            return
        stop_done.append(1)
        pwlog("stop marker: session/stop %s" % sid)
        try:
            c.request("session/stop", {"sessionId": sid}, timeout=30)
            # F36: the real stop is executed by THIS process and its
            # result never got back to the phone (the poller only says
            # "Frenando…" after its 4s grace) — the app confirmed →
            # component ack
            acks.choice_received(cfg, "stopped «%s»" % title,
                                     "the app confirmed the stop")
        except Exception:  # noqa: BLE001
            log_exc("errors", "session/stop failed (uncooperative stop)")
    try:
        if not sid:  # /task new: create the session NOW (it lives during the turn)
            sid, err = _create_session(c, ws)
            if err:
                uhttp.send(cfg, err)
                if ph:
                    uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> failed: %s"
                                     % (esc(title), err))
                return
            act = dict(act, sid=sid)
            set_worker_session(sid, act)
            _workers().rekey("nueva", sid)  # the /stop button points to the real sid
        model = act.get("model")
        mode = act.get("mode") or "yolo"
        snap_sel = None if sid in (None, "nueva") else _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # already the bot's: nothing to restore
        # F7++: skip NO-OP sets — every request returns the FULL history
        # (26MB ≈ 30s on a large session, x3 per turn). The mode is
        # validated against the STORE in addition to the act (queued acts
        # are old snapshots that don't carry mode_set).
        cur_sel = _current_sel(snap_sel)
        skip_model = bool(cur_sel) and cur_sel == (model or MODEL_SEL)
        skip_mode = mode in (act.get("mode_set"),
                             active_bot_session().get("mode_set"))
        err = _proto_ready(c, sid, model_sel=model, mode=mode,
                           skip_model=skip_model, skip_mode=skip_mode)
        pwlog("proto ready %.1fs (skip_model=%s skip_mode=%s)"
              % (time.time() - t_arr, skip_model, skip_mode))
        if err:
            # incident 18:10: this path was SILENT in the log (only the
            # message to the chat) — an hour of diagnosis over that nothing
            pwlog("proto failed: %s" % err[:150])
            audit.record(sid, "proto", err[:150])  # F30: correlation
            uhttp.send(cfg, err)
            if ph:
                uhttp._live_edit(cfg, ph, "❌ <b>«%s»</b> failed: %s"
                                 % (esc(title), err))
            return
        if not skip_mode:
            # record the applied mode: the next turn skips it
            try:
                set_active_bot_session({**active_bot_session(),
                                        **act, "mode_set": mode})
            except Exception:  # noqa: BLE001
                log_exc("errors", "mode persist failed (mode reapplies next turn)")
        if ph:
            uhttp._live_edit(cfg, ph,
                             "🧠 <b>«%s»</b> session ready — sending…"
                             % esc(title))
        if _stop_requested(sid):
            # /stop arrived during startup: don't even send the turn
            uhttp.send(cfg, "⏹️ «%s» stopped by /stop (before sending)." % esc(title))
            if ph:
                uhttp._live_edit(cfg, ph, "⏹️ <b>«%s»</b> stopped by /stop"
                                 % esc(title))
            return
        # F63: this turn's subagents, from the inside — spawn the watcher
        # BEFORE the prompt goes out (a Task call can arrive at any point;
        # the watcher waits for the spawn file + the rollout dir)
        if cfg.get("agent_stream") and not agent_live.watcher_up(sid):
            me = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "zcode-telegram.py")
            if not os.path.exists(me):
                me = os.path.join(config.HOME, ".zcode", "hooks",
                                  "zcode-telegram.py")
            try:
                subprocess.Popen(
                    [sys.executable, me, "--agent-live", sid, title,
                     str(int(time.time())), ws or os.getcwd()],
                    start_new_session=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            except OSError as _e:
                pwlog("agent-live spawn failed: %s" % _e)
        outcome = _run_turn(c, cfg, act, text, sid, ws, title, model,
                            stop_hook, ph=ph)
        # F8a: warm cycle — the runtime does NOT die with the turn: it
        # waits for THIS session's next prompt with the connection already
        # resumed (live-state pattern, ADR-014: the ~30s resume is paid
        # once and back-to-back messages start instantly).
        # warm_keepalive_s=0 → one process per turn, the usual behavior
        # (and the tests' default).
        keepalive = int(cfg.get("warm_keepalive_s") or 0)
        rotate_mb = float(cfg.get("rotate_threshold_mb") or 0)
        while keepalive > 0 and outcome in ("ok", "error"):
            # F8b: rotate ONLY with an empty queue (it never blocks a
            # user message) nor with the Mac in the session — and F28:
            # if one arrives DURING the dance, _rotation_deferred defers it
            if (rotate_mb > 0 and not _queue_peek(sid)
                    and sid not in running_turns()
                    and _transcript_mb(sid) > rotate_mb):
                rot_result = _rotate_session(c, cfg, act, sid, ws, title,
                                             model, snap_sel)
                if rot_result is not None:
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass  # silent-by-design: the old runtime retires either way
                    c, sid, act = rot_result
                    model = act.get("model")
                    snap_sel = None  # new session: nothing to restore
            item = _warm_wait(cfg, sid, keepalive)
            if item is None:
                break
            verbosity.refresh(cfg)  # F61: the WARM worker re-reads the
            # level — /verbosity applies on the NEXT turn, not on the
            # next process
            next_text, act2 = item
            if _interrupt_pending(sid):
                # F58: the redirect lands with its story — the model knows
                # it was interrupted mid-flight and reassesses instead of
                # resuming the dead plan
                next_text = _INTERRUPT_PREAMBLE + next_text
                _interrupt_consume(sid)
                pwlog("warm: pickup is an interrupt redirect (preamble on)")
            # the item carries a fresh act (/task may have changed model/mode)
            act, model = act2, act2.get("model")
            outcome = _run_turn(c, cfg, act, next_text, sid, ws, title,
                                model, stop_hook, ph=None)
    except Exception as e:
        # a dead worker is NEVER silent anymore: notify the chat + log
        import traceback
        pwlog("worker CRASH:", traceback.format_exc())
        audit.record(sid, "crash", str(e)[:150])  # F30: correlation
        try:
            uhttp.send(cfg, "⚠️ The worker of «%s» died processing your "
                  "message: <code>%s</code>" %
                  (esc(title), esc(str(e)[:150])), loud=True,
                  markup=_RETRY_KEYBOARD)
        except Exception:
            log_exc("errors", "crash report send ALSO failed (chat blind?)")
    finally:
        _active_turn_clear(sid)
        _workers().pop_if_mine(sid or "nueva")  # only our OWN (incident 15:40)
        if sid:
            _stop_consume(sid)  # F16: marker consumed — don't stop the next turn
            _interrupt_consume(sid)  # F58: the redirect story dies with the worker
        try:
            c.close()
        except Exception:
            log_exc("errors", "worker close failed")
        if sid and snap_sel:
            try:
                _restore_model(sid, snap_sel)
            except Exception:
                log_exc("errors", "worker restore failed")
        try:
            _queue_drain(cfg, sid)
        except Exception:
            log_exc("errors", "queue drain after close failed")


def _runtime_dead(c):
    """True if the worker's child runtime no longer exists (OOM/kill)."""
    p = getattr(c, "p", None)
    try:
        return p is not None and p.poll() is not None
    except OSError:
        return True


def _orphan_fetch_text(ent, sid):
    """F21: the answer text of an already-completed orphan turn.
    Ephemeral runtime: resume the sid and fetch — WITHOUT setModel/setMode
    so the session's configuration isn't clobbered. Never raises."""
    try:
        c = zc_proto.make(cwd=ent.get("ws") or config.DEFAULT_WS)
        try:
            # 120s: best-effort rescue in the poller's supervisor —
            # enough for large sessions without hanging the supervisor loop
            r = c.request("session/resume", {"sessionId": sid}, timeout=120)
            if "error" in r:
                return None
            msgs = _fetch_messages(c, sid, after_id=ent.get("last_id"),
                                   limit=120) or []
            return _extract_assistant_text(msgs, last_only=True).strip()
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass  # silent-by-design: closing the probe runtime
    except Exception:  # noqa: BLE001
        log_exc("errors", "orphan final-text fetch failed")
        return None


def _orphans_check(cfg):
    """F21 — orphan supervisor (runs on every poller cycle): bot turns
    whose worker died abruptly. The worker's finally always cleans up its
    record → an entry with a dead pid = death WITHOUT cleanup → an
    honest notice instead of eternal silence. Cheap: one read + one
    os.kill per entry. F30: also reaps the child Popens (zombies)."""
    _reap_children()
    # F69 — agent watchers die with every deploy while their background
    # agents run on: relaunch them (own throttle, single-flight inside)
    try:
        agent_live.respawn_orphans(cfg)
    except Exception as exc:  # noqa: BLE001 — never breaks the poller cycle
        pwlog("respawn_orphans failed: %s\n%s" % (exc, traceback.format_exc()))
    q = _store("bot-active.json").read()
    if not isinstance(q, dict) or not q:
        return
    now = time.time()
    for sid, ent in list(q.items()):
        if not isinstance(ent, dict):
            continue
        t0 = float(ent.get("t0") or 0)
        if now - t0 > 7200:  # 2h: silent purge (anti PID-reuse)
            _active_turn_clear(sid)
            continue
        pid = ent.get("pid")
        try:
            os.kill(int(pid), 0)
            continue  # alive: healthy worker, nothing to do
        except (OSError, ValueError, TypeError):
            pass  # silent-by-design: the liveness probe — the except IS the verdict (dead)
        # dead pid: did the turn finish before the worker went down?
        end = None
        for _ts, _s, _ev, d in zc_logs.iter_events(
                config.BASE, needles=("turn.completed", "turn.failed"),
                sids={sid}, since_epoch=t0, last_files=1,
                tail_bytes=zc_logs.LOG_TAIL):
            end = d
        title = entry_title(ent) or sid[-8:]
        by_stop = bool(ent.get("stop"))  # F47c: the user stopped it
        reason = None  # what the CoT cache closes with (None = normal)
        if end is not None and end.get("event") == "turn.completed":
            # REAL delivery of the text: read-only ephemeral runtime
            # (resume of the sid WITHOUT setModel — doesn't clobber the configuration)
            reply = _orphan_fetch_text(ent, sid)
            if reply:
                _send_long_reply(
                    cfg, title, (now - t0) / 60.0, reply)
                pwlog("orphan", sid[-8:], "delivered (%d chars)" % len(reply))
            else:
                uhttp.send(cfg, "🛟 The worker of «%s» died, but its turn "
                      "FINISHED — the reply stayed in the session (/cot "
                      "for the trace)." % esc(title))
                pwlog("orphan", sid[-8:], "end=completed (text not recoverable)")
        elif end is not None and by_stop:
            # /stop outlasted the grace → kill -9 → no finally: the turn
            # was left cancelled because the user asked for it, not «falló»  # noqa: spanish — quoted old banner
            uhttp.send(cfg, "⏹️ «%s» stopped by your /stop — it did not "
                       "answer the cooperative stop in time and was killed; "
                       "the turn stayed cancelled." % esc(title))
            pwlog("orphan", sid[-8:], "end=failed (by /stop)")
            audit.record(sid, "stop", "turn cancelled by /stop, worker "
                         "finished off when the grace expired")
            reason = "stopped by /stop"
        elif end is not None:
            uhttp.send(cfg, "❌ «%s» failed (the worker had died): %s"
                 % (esc(title), esc(str((end.get("error") or {})
                                         .get("message", ""))[:150])))
            pwlog("orphan", sid[-8:], "end=failed")
            audit.record(sid, "turn-failed",
                         "worker dead, the turn ended failed")
            reason = "worker dead, the turn ended failed"
        elif by_stop:
            uhttp.send(cfg, "⏹️ «%s» stopped by your /stop — the runtime "
                       "was killed before it could close the turn; no "
                       "reply will arrive." % esc(title))
            pwlog("orphan", sid[-8:], "no end (by /stop)")
            audit.record(sid, "stop", "worker finished off by /stop "
                         "with no turn end")
            reason = "stopped by /stop"
        else:
            uhttp.send(cfg, "⛔️ The turn of «%s» died with its worker "
                  "(kill/OOM) — no reply will arrive. Resend the message "
                  "whenever you want." % esc(title))
            pwlog("orphan", sid[-8:], "no end (runtime dead)")
            audit.record(sid, "dead-turn",
                         "worker died with no turn end (kill/OOM)")
            reason = "worker died with no turn end (kill/OOM)"
        if end is None:
            # F58: no CLI turn end will EVER arrive (the runtime died with
            # its worker): tombstone it or running_turns() holds a ghost
            force_turn_stopped(sid)
        # F47d: the turn's CoT cache kept the worker's last vivo=True —
        # its closing died with it; nobody else closes it here
        cotmod.close_cache(sid, t0, **({"error": reason} if reason else {}))
        _active_turn_clear(sid)
        # that session's queue was left dormant: wake it if it's the current one
        try:
            if _task_current_ok(cfg, sid):
                _queue_resume(cfg, sid, title)
        except Exception:  # noqa: BLE001
            log_exc("errors", "orphan queue wake failed")
