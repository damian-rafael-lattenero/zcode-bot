"""live_view — the sidecar of a headless turn (F4d).

LiveTurnView: the live view of the turn (live reasoning box, permanent
CoT boxes, steps box). It used to be the gateway's _live_view function
with 22 locals mutated across the loop and a closure with nonlocals —
the «Probando cot 1234» incident (14/10) was a forgotten nonlocal.
As a class, the bug disappears by construction: state lives in
attributes and cot_flush is a method.

Family: _turn_end_poll (reactive wait for the end of the turn), anti-OOM
guards (_poll_cadence, _rss_*, _live_skip_*), _headless_text and the
tool boxes. Transport BY ATTRIBUTE (uhttp.send): a single patching seam
for the whole suite.
"""
import json
import os
import subprocess
import time

from zcode_bot import config
from zcode_bot import cli_logs as zc_logs
from zcode_bot import reasoning as cotmod  # F42
from zcode_bot import fswatch
from zcode_bot.core import _stop_requested, _task_current_ok, _log_watch_paths
from zcode_bot.messages import (ToolStep, _fetch_messages, _last_id_of,
                                _tool_box, _tool_box_on, _tool_short_desc,
                                _tools_with_call_id)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, fmt_tok
from zcode_bot.utils.logging import pwlog

# ---------------------------------------------------------------- trace

COT_CHUNK = 3000   # per message: esc() can inflate the text, margin vs 4096
COT_TAIL = 3200    # tail that stays editable in the live box
EVENTS_TRIP = 1200  # replay larger than this → live OFF (anti-OOM, see LiveTurnView)
LIVE_FRAME_TRIP = 25 * 1024 * 1024  # a >25MB frame in an INCREMENTAL poll
# means afterSeq did not work and the server re-replays the whole session
# on EVERY poll (the regime that inflated a real turn to 3.2GB of RSS) →
# live OFF + per-session flag. The FIRST poll is exempt: the single
# startup replay is paid once and released (the CoT is shown in full)
LIVE_SKIP_TTL = 6 * 3600  # the flag lives 6 h; compact shrinks the replay
# and clears it (see zcode_compact), after which live comes back on its own
_LONG_TURN_NOTICE_S = config.LONG_TURN_NOTICE_S  # R0b/F51c: how often to
# notify "⏱️ lleva N min" during the UNBOUNDED wait for the end of the turn
# (timeout=None of _turn_end_poll). Lives in config.turno_largo_aviso_s
# because the Mac watcher (reasoning) notifies with the SAME value.
LIVE_BIG_FRAME = 2 * 1024 * 1024  # frame >2MB → slow down the poll. F7b: the
# poll is incremental (afterSeq — verified on the app-server; afterEventId
# IS rejected: ZodError unrecognized_keys), so a large frame is already
# abnormal (snapshot/replay) and all that's left is damping the frequency


def _frame_size(r):
    """Size of THIS poll's frame (not the connection's max: the giant
    resume/setModel responses inflated max_frame and the trip was killing
    a PERFECTLY healthy event stream by inheritance —
    incident 15/10: 'live evs=0 frame=25MB' → CoT paused for no reason)."""
    try:
        return len(json.dumps(r))
    except (TypeError, ValueError):
        return 0


def _poll_cadence(max_frame):
    """Cadence of session/events based on the observed size. F7b: with
    afterSeq the poll is incremental and frames stay in KBs; a large
    frame is a sign of an unexpected replay → lower the frequency."""
    return 5.0 if max_frame > LIVE_BIG_FRAME else 2.0


def _rss_now_mb(run=None):
    """CURRENT RSS of the process (ps) — ru_maxrss is a historical peak and
    does not tell transient from retained; this does. The poller does NOT
    run under the sandbox (only its runtimes do), so ps works for it."""
    run = run or subprocess.run
    try:
        out = run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                  capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) // 1024
    except (OSError, ValueError):
        return -1


def _rss_mb():
    """Maximum RSS of the process in MB (ru_maxrss comes in bytes on macOS)."""
    import resource
    try:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576)
    except Exception:
        return -1


def _stream_end(evs, mine, t_send, last_seq):
    """F9 — one poll of the stream → (end|None, mine, last_seq).

    The session/events stream carries the TURN's lifecycle: `type`
    turn.started (payload with inputId/input/executionStartedAt) and
    turn.completed/failed (payload with inputId/resultType/response,
    spike scripts/spike_f9_turnfin.py). The stream does NOT carry turnId
    (the key of the CLI logs): the F20 correlation is rebuilt with
    inputId — the first turn.started after our session/send sets mine,
    and only the completed/failed with THAT inputId counts."""
    end_ev = None
    for ev in evs:
        sq = ev.get("seq")
        if isinstance(sq, int) and (last_seq is None or sq > last_seq):
            last_seq = sq
        et = ev.get("type") or ""
        if et not in ("turn.started", "turn.completed", "turn.failed"):
            continue
        p = ev.get("payload") or {}
        if et == "turn.started":
            if mine is not None:
                continue
            ts0 = (p.get("executionStartedAt") or 0) / 1000.0
            if t_send is None or ts0 >= t_send - 1:
                mine = p.get("inputId") or p.get("queryId")
        elif mine is not None:
            # the identifier can arrive as queryId on either end
            # (mine already resolved it on open): compare the same
            # field, don't assume inputId
            if (p.get("inputId") or p.get("queryId")) == mine:
                end_ev = ev
        elif t_send is None:
            end_ev = ev
    if end_ev is None:
        return None, mine, last_seq
    p = end_ev.get("payload") or {}
    ok = end_ev.get("type") == "turn.completed" \
        and str(p.get("resultType") or "success") == "success"
    return ({"event": "turn.completed" if ok else "turn.failed",
             "error": {} if ok else {"message": str(
                 (p.get("error") or {}).get("message") if isinstance(
                     p.get("error"), dict) else
                 (p.get("error") or p.get("resultType") or "turn failed")
             )[:150]}}, mine, last_seq)


def _turn_end_poll(sid, t0, timeout=1800, stop_check=None, t_send=None,
                   conn=None, start_seq=None, notice=None):
    """Waits for turn.completed/failed of sid (ts >= t0).

    F9 — BY EVENTS when there is a connection and cursor (conn+start_seq
    from the subscribe made before the send): incremental afterSeq poll
    (frames in KB, the same anti-OOM discipline as the live view). The
    stream does not carry turnId: correlation is by inputId
    (_stream_end). Anything that fails (dead conn, request in error,
    giant frame = replay) degrades to the usual path: scanning the CLI's
    JSONL logs (512KB tail) — which remains the ONLY path when there is
    no conn (and for the orphan supervisor, which sees turns of workers
    that are already dead).

    F20 — correlation by turnId on the logs path: with t_send (post
    session/send accepted), the first turn.started with ts >= t_send is
    THE worker's turn, and we wait for THAT turnId to end (incident
    18:29: it delivered the Mac's text as its own).

    REACTIVE (F16): on the logs path the thread sleeps in the kernel
    (kqueue) and wakes up when the CLI writes. stop_check() runs on
    every iteration (the hook for /stop by marker).

    R0b — timeout=None = UNBOUNDED: as long as the turn lives, we wait
    (giving up at 40 min was killing the runtime: turn.failed "Turn was
    cancelled" the very second the worker exited — 20/10 14:55). The
    only cutoffs remain the end of the turn or the /stop of stop_check.
    notice(min) is called every _LONG_TURN_NOTICE_S of elapsed turn."""
    deadline = None if timeout is None else time.time() + timeout
    mine = None
    last_seq = start_seq
    last_scan = 0.0
    next_notice = t0 + _LONG_TURN_NOTICE_S
    while deadline is None or time.time() < deadline:
        if stop_check is not None:
            try:
                stop_check()
            except Exception:  # noqa: BLE001
                pass
        if notice is not None:
            now_ts = time.time()
            if now_ts >= next_notice:
                try:
                    notice(int((now_ts - t0) // 60))
                except Exception:  # noqa: BLE001 — the notice never cuts the wait
                    pass
                while next_notice <= now_ts:  # clock jumped far ahead: catch up
                    next_notice += _LONG_TURN_NOTICE_S
        if conn is not None:
            try:
                params = {"sessionId": sid}
                if last_seq is not None:
                    params["afterSeq"] = last_seq
                r = conn.request("session/events", params, timeout=20)
                size = _frame_size(r)
                if size > LIVE_FRAME_TRIP:
                    # giant replay in the end-wait poll = broken afterSeq:
                    # the regime that inflates RSS — degrade to logs
                    pwlog("end: stream frame %.1fMB — fallback to logs"
                          % (size // 1048576))
                    conn = None
                else:
                    evs = (r.get("result") or {}).get("events") or []
                    end_ev, mine, last_seq = _stream_end(
                        evs, mine, t_send, last_seq)
                    if end_ev is not None:
                        return end_ev
            except Exception:  # noqa: BLE001 — the end-wait never breaks the turn
                conn = None  # stream down: logs for the rest of the wait
        if conn is None:
            now = time.time()
            if now - last_scan >= 1.0:  # throttle: the log writes in bursts
                last_scan = now
                end_ev = None
                for _ts, _s, _ev, d in zc_logs.iter_events(
                        config.BASE, needles=("turn.started", "turn.completed",
                                       "turn.failed"),
                        sids={sid}, since_epoch=t0, last_files=1,
                        tail_bytes=zc_logs.LOG_TAIL):
                    ev = d.get("event")
                    if ev == "turn.started":
                        if mine is None and (t_send is None
                                             or _ts >= t_send - 1):
                            mine = d.get("turnId")
                    elif ev in ("turn.completed", "turn.failed"):
                        if d.get("turnId") == mine or (mine is None
                                                       and t_send is None):
                            end_ev = d
                if end_ev is not None:
                    return end_ev
            # reactive recv of the mailbox: wake up on the next log write
            # (fswatch's debounce coalesces bursts)
            wait_s = 4.0 if deadline is None else \
                max(0.05, min(4.0, deadline - time.time()))
            fswatch.wait_change(_log_watch_paths(), timeout=wait_s)
        else:
            # R0b: deadline None = unbounded — the pacing is not shortened
            time.sleep(2.0 if deadline is None else
                       max(0.05, min(2.0, deadline - time.time())))


def _live_skip_path():
    return os.path.join(config.STATE_DIR, config.LIVE_SKIP_FILE)


def _live_skip_all():
    try:
        with open(_live_skip_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _live_skip_write(d):
    """R4 — the atomic write of the flag (tmp + replace) that
    _live_skip_mark and _live_skip_clear used to duplicate. Best-effort.
    tmp BY PID (audit 20/10): two workers marking at once interleaved the
    same tmp and the replace could publish corrupt JSON — the flags of
    ALL sessions were lost (the OOM regime they prevent)."""
    tmp = "%s.tmp.%d" % (_live_skip_path(), os.getpid())
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _live_skip_path())
    except OSError:
        pass


def _live_skip_mark(sid, now=None):
    """Marks the session as 'giant replay': the next turn starts with the
    live CoT OFF (not even a GBs parse — steps and the final document go
    on as usual). Idempotent and best-effort."""
    now = now or time.time()
    d = _live_skip_all()
    d[sid] = now
    _live_skip_write(d)


def _live_skip_check(sid, now=None):
    """True if the session has a fresh flag (TTL LIVE_SKIP_TTL)."""
    ts = _live_skip_all().get(sid)
    return bool(ts) and (now or time.time()) - ts < LIVE_SKIP_TTL


def _live_skip_clear(sid):
    d = _live_skip_all()
    if sid in d:
        del d[sid]
        _live_skip_write(d)


HEADLESS_GUARD = (
    "\n\n—\n⚠️ (notice from the bot running this message: you are running "
    "HEADLESS inside your own zcode runtime. Do NOT kill or restart zcode "
    "processes — kill/pkill/killall on zcode or app-server kill YOUR OWN "
    "host and this turn dies with «Turn was cancelled» (real incidents "
    "2026-10-13 14:04, 15:12 and 16:13). launchctl is already BLOCKED in "
    "your sandbox: don't even try. If the bot or the poller look stuck, do "
    "NOT restart them: tell the user over the chat and have them use "
    "/nuclear from Telegram. To deploy changes: commit + push and tell "
    "them — deploy.sh from here only delays the reload.)"
)


def _headless_text(text, cfg):
    """Final text of the headless prompt: anti-runtime-suicide guardrail.

    Incident 15:12: a headless turn ran `kill -TERM <pid>` on its own
    runtime (it believed it was a "duplicated worker") and cancelled
    itself. It is APPENDED (not prepended) so the start of the prompt is
    untouched, and deduplicated in case the text already carries it
    (queue re-send).
    """
    if not cfg.get("headless_guard"):
        return text
    if HEADLESS_GUARD[:20] in (text or "")[-600:]:
        return text
    return (text or "") + HEADLESS_GUARD


def _cot_chunks_to_send(cot_full, flushed, tail_keep=COT_TAIL,
                        ch=COT_CHUNK):
    """Chunks ready to send as PERMANENT CoT messages: everything
    accumulated beyond the live tail (tail_keep), in pieces of ch. The
    tail keeps being edited separately — that way the CoT stays
    scrolleable in the chat while the turn runs (the 4096/message limit
    is why the chunks exist). UNBOUNDED: Damian asked that ALL of them
    be sent (the 1.2 s pace of the flush keeps the chat within
    Telegram's ~1 msg/s)."""
    out = []
    while len(cot_full) - flushed > tail_keep:
        chunk = cot_full[flushed:flushed + ch]
        out.append(chunk)
        flushed += len(chunk)
    return out, flushed


def _boxes_new_step(cfg, title, counter, new_items, boxed=None):
    """F22bis — sends every new tool call as PERMANENT messages (scrolleable
    like the CoT ones): full command + diff. Config tools_stream=false
    turns it off.
    F7c: `new_items` are (callID, name, inp, status); the tools in `boxed`
    already have their own live box (they were born 🔵 from the event
    stream and get closed by tool.updated.result) — don't re-send the
    full box."""
    if not cfg.get("tools_stream"):
        return
    boxed = boxed or set()
    for cid, name, inp, _status in new_items:
        if cid and cid in boxed:
            continue
        if not _tool_box_on(name, cfg):  # F7c+: box preset
            continue
        counter[0] += 1
        try:
            uhttp.send(cfg, "%s · <b>%d</b>" % (_tool_box(name, inp),
                                                counter[0]), loud=False)
            if counter[0] % 3 == 0:
                time.sleep(1.0)  # ~1 msg/s pace of the chat
        except Exception:  # noqa: BLE001
            pass


class LiveTurnView:
    """Sidecar of a headless turn. Five live/permanent messages:
    1) live box of reasoning/text (session/events deltas, dedupe by
       eventId + afterSeq cursor F7b, without accumulating payloads —
       the size guards of the leak) with the current tool alongside
       (F7c).
    2) PERMANENT CoT boxes: when the tail fills up (4096/message is
       Telegram's hard limit) the excess is sent as fixed messages
       «CoT N» — the reasoning stays scrolleable from start to end
       WHILE the turn runs (config cot_stream=false turns it off).
    3) «steps»: the completed tool calls from the history
       (afterMessageId) — 🔧 commands, files, tools.
    4) F7c "live action": one box per tool that is BORN 🔵 as soon as
       the input completes (tool_call from the stream, input already
       parsed — full command / real diff via _tool_box) and is EDITED
       to ✅/❌ with tool.updated.result. The current tool is seen being
       typed in the live box.
    5) F42-F46 «CoT en botones» (CoT in buttons): ONE BUTTON BOX PER  # noqa: spanish — the feature's original name, quoted
       REASONING ROUND (labels «CoT R.C») that is born with the first
       sealed box of that round in its chronological position, grows
       only while it lives and closes with its own final state. F49: no
       global index at the end — the boxes left without a message go in
       ONE latecomers message.

    deps: every external effect is injectable (send/live_send/live_edit/
    sleep/now/pwlog/fetch/task_current_ok/stop_requested/skip_check/
    skip_mark/steps_boxes/poll_cadence/rss/rss_now) — testable without a
    network and without patching the global time.sleep (F4d).
    """

    def __init__(self, c, cfg, sid, t0, title, msg_id, th, last_id=None,
                 deps=None, start_seq=None):
        d = deps or {}
        self.c, self.cfg, self.sid = c, cfg, sid
        self.t0, self.title, self.th = t0, title, th
        # --- injectable deps (defaults = module; tests pass recorders) ---
        self.send = d.get("send", uhttp.send)
        self.live_send = d.get("live_send", uhttp._live_send)
        self.live_edit = d.get("live_edit", uhttp._live_edit)
        self.sleep = d.get("sleep", time.sleep)
        self.now = d.get("now", time.time)
        self.pwlog = d.get("pwlog", pwlog)
        self.fetch_messages = d.get("fetch_messages", _fetch_messages)
        self.task_current_ok = d.get("task_current_ok", _task_current_ok)
        self.stop_requested = d.get("stop_requested", _stop_requested)
        self.skip_check = d.get("skip_check", _live_skip_check)
        self.skip_mark = d.get("skip_mark", _live_skip_mark)
        self.steps_boxes = d.get("steps_boxes", _boxes_new_step)
        self.poll_cadence = d.get("poll_cadence", _poll_cadence)
        self.rss = d.get("rss", _rss_mb)
        self.rss_now = d.get("rss_now", _rss_now_mb)
        # --- turn context ---
        self.msg_id = msg_id
        self.deadline = time.time() + 2100
        # --- stream state (former locals of _live_view) ---
        self.reason = ""      # only the IN-PROGRESS thinking phase (gets reset)
        self.reason_n = 0     # total chars thought (for the log)
        self.text = ""      # TAIL of what was written (last 8KB, live box)
        self.text_n = 0     # total chars written
        self.n_deltas = 0
        self.seen = set()
        self.last_seq = start_seq  # F7b: afterSeq cursor (the eventSeq of
        # the subscribe if the worker passed it — skips the startup replay)
        self.tools_live = {}  # F7c: toolCallId → {name, text, inp,
        # msg_id, end_ev, salida} — the lifecycle as seen by the stream
        self.steps = []
        self.steps_nmsg = [0]  # 1-element list: that's how _boxes_new_step consumes it
        self.ph_steps = None
        self.steps_last = last_id
        self.last_edit = 0.0
        self.last_poll_steps = 0.0
        self.cot_full = ""    # CoT pending to send (what's sent is RELEASED)
        # F42 (Damian: the PHONE's CoT in buttons too): every thinking
        # phase that CLOSES is a block → button. cot_full is NOT released
        # in buttons mode (_cot_flush no-op): the cuts by block index need
        # the whole, intact text.
        self.cot_boxes = []      # F43: boxes SEALED by size
        self.cot_labels = []    # F46: «CoT R.C» — round R, box C
        self.cot_sell = cotmod.CotSealer()  # seals at the cap, restarts per run
        self.cot_ph = None      # F46: the button box of the IN-PROGRESS round
        self.cot_ph_last = None  # …and the last one born (the close edits it)
        self.cot_phs = []       # F46: message_id per box (the tap edits ITS message)
        self.cot_round = 1   # F46: round in progress (for the R.C label)
        self.cot_round_ini = 1    # global index of its first box
        self.cot_round_n = 0      # sealed boxes of the round in progress
        self.cot_flushed = 0  # chars already sent as permanent messages
        # PUBLISHED CoT messages: text chunks in stream mode, button boxes
        # born in buttons mode (edits don't count)
        self.cot_nmsg = 0
        self.live_id = msg_id  # live box: MOVES at the first CoT flush
        self.live_moved = False
        self.text_phase_noted = False
        self.first_poll = True  # anti-OOM instrumentation (first replay)
        self.events_off = False  # anti-OOM: session/events returns the
        # session's FULL replay on every call; on giant sessions, parsing
        # it every 2s inflates the RSS until macOS kills the poller
        # (2.9GB real, turn lost).
        if self.skip_check(sid):
            # flag from a previous turn: THIS session's replay already
            # weighs GBs (images) — not a single parse; steps and document go on
            self.events_off = True
            self.pwlog("live events OFF from startup (giant replay flag)")
        self.cad = 2.0  # poll cadence (adaptive: large frame → 5 s)
        self.background = False  # F15: stopped being current mid-turn
        self.stop_requests = 0       # F16: /stop by marker — only ONCE

    # ------------------------------------------------- CoT flush

    def _cot_flush(self, tail_keep, pace=True):
        """Sends the accumulated CoT as PERMANENT messages (boxes that stay
        scrolleable above). Paused to respect the ~1 message/s per chat of
        the Telegram FAQ. UNBOUNDED: all of them are sent."""
        if not self.cfg.get("cot_stream") or self.cfg.get("cot_buttons"):
            return  # off, or F42: in buttons mode the text is not streamed
        chunks, nf = _cot_chunks_to_send(
            self.cot_full, self.cot_flushed, tail_keep=tail_keep)
        for i, chunk in enumerate(chunks):
            if i and pace:
                self.sleep(1.2)
            self.cot_nmsg += 1
            self.send(self.cfg, "🧠 <b>«%s»</b> · CoT %d\n<pre>%s</pre>"
                      % (esc(self.title), self.cot_nmsg, esc(chunk)),
                      loud=False)
        if nf:
            # MEMORY: what was already delivered to the chat is not
            # retained — the sent prefix is released and the final
            # document comes from the history
            self.cot_full = self.cot_full[nf:]
            self.cot_flushed = 0
        if chunks and not self.live_moved and self.live_id:
            # the "live" one moves to the end of the chat: the live box
            # stays BELOW the frozen boxes, where the reading is happening
            self.live_moved = True
            self.live_id = (self.live_send(
                self.cfg, "🧠 <b>«%s»</b> — thinking…"
                % esc(self.title)) or self.live_id)

    # ------------------------------------------------- F42: CoT in buttons

    def _cot_save(self, **extra):
        """The turn's cache, ALWAYS with the ph of the button box if it was
        already born (F43c: the tap edits that message — a rewrite without
        ph lost it and the tap fell down) and with the phs PER BOX (F46:
        every round has its own message; the tap edits the one for ITS
        round)."""
        ph = self.cot_ph or self.cot_ph_last
        if ph:
            extra["ph"] = ph
        extra["phs"] = list(self.cot_phs)
        try:
            cotmod.save_cache(self.sid, int(self.t0), self.title,
                          self.cot_boxes, self.cot_labels, **extra)
        except OSError:
            pass

    @property
    def _cot_active(self):
        """R4 — the button-box paths only run with the knob on and WITHOUT
        having gone to the background (F15): the guard that used to repeat
        on every entry."""
        return bool(self.cfg.get("cot_buttons")) and not self.background

    def _cot_box_new(self, box, label):
        """F43: a box FILLED UP (or the round closed and the tail gets
        sealed): a new button NOW — boxes appear as they are created.
        F46: the button box is PER ROUND — it is born with the first
        sealed box of each round, in its chronological position (among
        that phase's tools), and grows only while that round lives; the
        next round is born in a NEW message. Its message_id is needed
        (send returns it; with a True here every edit failed and the box
        got stuck — what Damian saw)."""
        if not self._cot_active:
            return
        self.cot_boxes.append(box)
        self.cot_labels.append(label)
        self.cot_round_n += 1
        self._cot_save(live=True)  # before the button: the tap already finds it
        pend, k = self.cot_sell.in_progress
        text = cotmod.live_text(
            self.title, self.cot_round_n,
            "%d.%d" % (self.cot_round, k), pend)
        kb = {"inline_keyboard": cotmod.box_buttons(
            self.sid, int(self.t0), self.cot_labels[self.cot_round_ini - 1:],
            start=self.cot_round_ini)}
        if self.cot_ph:
            if self.live_edit(self.cfg, self.cot_ph, text, markup=kb):
                cotmod.kb_log("phone live r%d" % self.cot_round,
                                  self.cot_ph, kb)
        else:
            self.cot_ph = cotmod.ph_of(
                self.send(self.cfg, text, loud=False, markup=kb))
            if self.cot_ph:
                self.cot_ph_last = self.cot_ph
                self.cot_nmsg += 1
                cotmod.kb_log("phone born r%d" % self.cot_round,
                                  self.cot_ph, kb)
        self.cot_phs.append(self.cot_ph)
        if self.cot_ph:
            self._cot_save(live=True)  # ph + phs up to date for the tap

    def _cot_run_close(self):
        """A round ended (it moves on to writing / calls a tool): the tail
        is sealed as the last box and ITS button box keeps its own final
        state; the next round is born in a NEW message (F46, Damian's
        idea: each batch of little boxes at the moment of the turn when
        it happened, not everything piled up above)."""
        if not self._cot_active:
            return
        r = self.cot_round
        for box, n in self.cot_sell.close():
            self._cot_box_new(box, "CoT %d.%d" % (r, n))
        if not self.cot_round_n:
            return  # F48c: round with no boxes — consumes no number, no message
        if self.cot_ph:
            ini = self.cot_round_ini
            end_ev = ini + self.cot_round_n
            # R9: the closing formula lives in reasoning (shared with
            # the Mac watcher); the transport is the one from here
            text, kb = cotmod.reasoned_close(
                self.sid, int(self.t0), self.title, r,
                self.cot_boxes, self.cot_labels, ini, end_ev)
            if self.live_edit(self.cfg, self.cot_ph, text, markup=kb):
                cotmod.kb_log("phone close r%d" % r, self.cot_ph, kb)
        self.cot_round = r + 1
        self.cot_ph = None       # the next round is born in a new message
        self.cot_round_ini += self.cot_round_n
        self.cot_round_n = 0

    def _cot_empty(self):
        """F45 — did it close WITHOUT reasoning and should that be said?
        Only if the stream was seen whole (there were deltas and no
        anti-OOM trip) and nobody stopped the turn: with no deltas it is
        a turn of the Mac app (its stream does not cross to the bot) and
        with /stop the silence is the user's."""
        return bool(self.cfg.get("cot_empty_notice")) and self.n_deltas > 0 \
            and self.reason_n == 0 and not self.events_off \
            and not self.stop_requests

    # ------------------------------------------------- events phase

    def _poll_events(self):
        if self.events_off:
            # trip activated: don't ask for the giant replay again — the
            # steps and the final document cover the rest of the turn
            return
        params = {"sessionId": self.sid}
        if self.last_seq is not None:
            # F7b: INCREMENTAL poll — the server only returns seq > afterSeq
            # (spike F7a: verified, 0 events past the cursor). The dedupe
            # by eventId remains as a safety net against a replay.
            params["afterSeq"] = self.last_seq
        r = self.c.request("session/events", params, timeout=20)
        evs = (r.get("result") or {}).get("events") or []
        size = _frame_size(r)
        is_first = self.first_poll
        if self.first_poll:
            self.first_poll = False
            self.cad = self.poll_cadence(size)
            self.pwlog("live evs=%d frame=%.1fMB rss=%dMB rss_now=%dMB cad=%.0fs"
                       % (len(evs), size // 1048576, self.rss(),
                          self.rss_now(), self.cad))
            # F7+++: the FIRST poll may bring the full replay ONCE (no
            # cursor): it gets parsed, shown and released — do NOT trip
            # (Damian: the CoT should ALWAYS be seen whole; the danger was
            # the replay EVERY 2 s of the pre-afterSeq regime, not one time).
        if not is_first:
            if size > LIVE_FRAME_TRIP:
                # a GIGANTIC frame in an incremental poll = the cursor is
                # not working (the server re-replays): this is the regime
                # that inflated the RSS — cut off and flag the session
                self.events_off = True
                self.skip_mark(self.sid)
                self.pwlog("live events OFF: %.1fMB frame in INCREMENTAL "
                           "poll (afterSeq broken) — flag %dh per session"
                           % (size // 1048576, LIVE_SKIP_TTL // 3600))
            elif len(evs) > EVENTS_TRIP:
                self.events_off = True
                self.pwlog("live events OFF: replay %d evs (anti-OOM) — "
                           "going on with steps+document" % len(evs))
            if self.events_off:
                return
        # F7b: the cursor advances with the max seq SEEN (even if the
        # event gets filtered out below by size/dedupe)
        for ev in evs:
            sq = ev.get("seq")
            if isinstance(sq, int) and (self.last_seq is None
                                        or sq > self.last_seq):
                self.last_seq = sq
        # buffer re-sent whole on every call: only the NEW ones
        for ev in evs[-400:]:
            eid = ev.get("eventId")
            if not eid or eid in self.seen:
                continue
            self.seen.add(eid)
            if len(json.dumps(ev)) > 30000:  # snapshot: ignore
                continue
            p = ev.get("payload") or {}
            k = p.get("kind") or p.get("type") or ""
            if k == "reasoning_delta" and p.get("delta"):
                dd = p["delta"]
                self.reason = (self.reason + dd)[-8192:]  # tail: the full text
                self.reason_n += len(dd)                  # lives in cot_full
                self.cot_full += dd
                self.n_deltas += 1
                for _box, _n in self.cot_sell.feed(dd):
                    # F43: at the cap; F46: R.C label of the round in progress
                    self._cot_box_new(_box,
                                         "CoT %d.%d" % (self.cot_round, _n))
            elif k == "text_delta" and p.get("delta"):
                dd = p["delta"]
                self.text = (self.text + dd)[-8192:]  # tail for the live box;
                self.text_n += len(dd)                  # the final document
                self.n_deltas += 1                       # comes from the store
                if self.reason:
                    # PHASE EVENT: went from thinking to writing. In buttons
                    # mode (F42) the block closes and the button box already
                    # counts it — no extra phase message. F48c: EVERY round
                    # closes here (before only the first one did: the
                    # text_phase_noted guard never reset → the tail of the 2nd
                    # onwards stayed in the sealer and the following round
                    # MERGED into it under the same R)
                    self._cot_run_close()
                    if not self.text_phase_noted:
                        # the phase notice of text mode: ONCE per turn
                        self.text_phase_noted = True
                        if self.cfg.get("cot_stream") \
                                and not self.cfg.get("cot_buttons") \
                                and not self.background:
                            self.send(self.cfg,
                                      "🧠 <b>«%s»</b> — analysis done "
                                      "(%s chars of CoT · %d boxes)\n✍️ now "
                                      "writing the answer…"
                                      % (esc(self.title),
                                         fmt_tok(len(self.cot_full)),
                                         self.cot_nmsg), loud=False)
                self.reason = ""  # started writing: the CoT has served its purpose
            elif k in ("tool_input_start", "tool_input_delta",
                       "tool_input_end", "tool_call",
                       "started", "result", "batch"):
                if k == "tool_input_start" and self.reason:
                    self._cot_run_close()  # F43: thought → about to call
                    # F48c: the thinking phase ended — tools that follow
                    # without new thinking don't re-close (each empty
                    # re-close consumed an R number: «CoT 34.3» with 19 rounds)
                    self.reason = ""
                # F7c: tool lifecycle via the stream (spike F7a:
                # tool_input_start→deltas→tool_call{input already parsed}→
                # tool.updated.started→tool.updated.result{success,content})
                self._tool_event(k, p)

    # ------------------------------------------- tool lifecycle

    def _tool_event(self, k, p):
        """F7c — updates tools_live according to the stream event. The
        permanent boxes are born 🔵 when the input completes (tool_call
        brings the input ALREADY parsed) and are EDITED to ✅/❌ with
        tool.updated.result."""
        if not self.cfg.get("tools_stream"):
            return
        tc = p.get("toolCallId")
        if not tc or k in ("started", "batch", "tool_input_end"):
            return  # started/batch add nothing visible; end is implicit
        e = self.tools_live.get(tc)
        if e is None:
            e = self.tools_live[tc] = {"name": "tool", "text": "",
                                       "inp": None, "msg_id": None,
                                       "mark": None, "output": ""}
        if k == "tool_input_start":
            e["name"] = str(p.get("toolName") or e["name"])
        elif k == "tool_input_delta" and p.get("delta"):
            # the input arrives as it is being typed (raw JSON): tail for the live box
            e["text"] = (e["text"] + p["delta"])[-4000:]
        elif k == "tool_call":
            e["name"] = str(p.get("toolName") or e["name"])
            if isinstance(p.get("input"), dict):
                e["inp"] = p["input"]
            # steps index right away (don't wait for the message poll)
            if e["inp"] is not None:
                self.steps.append(ToolStep(
                    e["name"], _tool_short_desc(e["name"], e["inp"])))
                if not self.background \
                        and _tool_box_on(e["name"], self.cfg):
                    self.steps_nmsg[0] += 1
                    try:
                        e["msg_id"] = self.live_send(
                            self.cfg, "%s · <b>%d</b>"
                            % (self._box_text(e), self.steps_nmsg[0]))
                    except Exception:  # noqa: BLE001
                        pass
                    if self.steps_nmsg[0] % 3 == 0:
                        self.sleep(1.0)  # ~1 msg/s pace of the chat
        elif k == "result":
            res = p.get("result") or {}
            e["mark"] = "✅" if res.get("success") else "❌"
            e["output"] = str(res.get("content") or "")[:800]
            if e.get("msg_id") and not self.background:
                try:
                    self.live_edit(self.cfg, e["msg_id"], self._box_text(e))
                except Exception:  # noqa: BLE001
                    pass
        # anti-growth cap: keep the last 60 (closed ones first — their
        # box already lives in the chat, the state is no longer needed)
        if len(self.tools_live) > 60:
            closed = [t for t, d in self.tools_live.items() if d.get("mark")]
            for t in closed[:len(self.tools_live) - 60]:
                del self.tools_live[t]

    def _box_text(self, e):
        """Text of the tool's box with its state mark (🔵 running /
        ✅ / ❌) and the output when there was one."""
        base = _tool_box(e["name"], dict(e.get("inp") or {}))
        base = base.replace("</b>", "</b> %s" % (e.get("mark") or "🔵"), 1)
        if e.get("output") and "└" not in base:
            base += "\n└ <pre>%s</pre>" % esc(e["output"])
        return base

    def _live_tool_line(self):
        """Line of the current tool for the live box: the command being
        typed (input_streaming) or 🔵 running — the "live action"."""
        if not self.cfg.get("tools_stream"):
            return ""
        for _tc, e in reversed(list(self.tools_live.items())):
            if e.get("mark"):
                continue
            if e.get("inp") is not None:
                d = _tool_short_desc(e["name"], e["inp"])
                return ("\n🔧 <b>%s</b> 🔵 running — <code>%s</code>"
                        % (esc(e["name"]), esc(d[:150])))
            if e["text"]:
                return ("\n🔧 <b>%s</b> typing the command:\n<pre>%s</pre>"
                        % (esc(e["name"]), esc(e["text"][-220:])))
            return "\n🔧 <b>%s</b>…" % esc(e["name"])
        return ""

    # ------------------------------------------------- steps phase

    def _poll_steps(self):
        self.last_poll_steps = self.now()
        try:
            new_items = self.fetch_messages(self.c, self.sid,
                                         after_id=self.steps_last, limit=30)
            if new_items:
                self.steps_last = _last_id_of(new_items) or self.steps_last
                with_id = _tools_with_call_id(new_items)
                # F7c: tools already added to the index by the stream
                # (tool_call) are not duplicated when the history brings them
                self.steps.extend(
                    ToolStep(n, _tool_short_desc(n, i))
                    for cid, n, i, _st in with_id
                    if not (cid and cid in self.tools_live))
                # F7c: close 🔵 boxes whose stream result was lost
                # (events_off mid-tool) — the history brings the output
                for cid, name, inp, status in with_id:
                    e = self.tools_live.get(cid)
                    if not (e and e.get("msg_id") and not e.get("mark")):
                        continue
                    e["mark"] = "❌" if str(status) == "error" else "✅"
                    out = inp.get("output") or inp.get("result") or ""
                    if isinstance(out, str) and out.strip():
                        e["output"] = out[:800]
                    try:
                        self.live_edit(self.cfg, e["msg_id"],
                                       self._box_text(e))
                    except Exception:  # noqa: BLE001
                        pass
                # F22bis: PERMANENT boxes for each new tool call (full
                # command / diff) — `new_items` is already incremental (after_id);
                # boxed = the ones already born 🔵 from the stream
                if not self.background:
                    try:
                        self.steps_boxes(self.cfg, self.title,
                                        self.steps_nmsg, with_id,
                                        boxed={t for t, d in
                                               self.tools_live.items()
                                               if d.get("msg_id")})
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------- live box

    def _live_box_body(self, elapsed):
        """R10 — the TEXT of the live box according to the turn's phase
        (pure: state → string, no I/O; _edit_live_box publishes it).
        Branches: writing, stream paused by anti-OOM, thinking (state
        with F43b buttons / raw text with cot_stream), Mac-app turn (no
        deltas: its streaming does not cross over) and plain thinking."""
        if self.text:
            return ("⚙️ <b>«%s»</b> · %ds\n<pre>%s</pre>\n✍️ writing…"
                    % (esc(self.title), elapsed, esc(self.text[-3200:])))
        if self.events_off:
            return ("📄 <b>«%s»</b> · %ds\nLive CoT paused "
                    "(the incremental stream failed — memory "
                    "protection; /compact reactivates it).\n"
                    "🔧 active steps · the final document carries the "
                    "CoT in FULL."
                    % (esc(self.title), elapsed))
        if self.reason:
            if self.cfg.get("cot_buttons"):
                # F43b (Damian: "besides the button, the CoT is visible"):
                # with buttons the content lives ONLY in the boxes — here
                # goes the state, not the duplicated raw text
                pend, k = self.cot_sell.in_progress
                return ("🧠 <b>«%s»</b> · %ds\nthinking… · %d box%s · "
                        "filling CoT %d.%d (%d chars)"
                        % (esc(self.title), elapsed, len(self.cot_boxes),
                           "es" if len(self.cot_boxes) != 1 else "",
                           self.cot_round, k, pend))
            return ("🧠 <b>«%s»</b> · %ds\n<b>thinking:</b>\n<pre>%s</pre>"
                    % (esc(self.title), elapsed, esc(self.reason[-3200:])))
        if self.n_deltas == 0 and elapsed > 12:
            # no deltas = the Mac app is running the turn (task opened
            # there): its internal streaming does not cross to the bot
            return ("🖥️ <b>«%s»</b> · %ds\nrunning on the Mac "
                    "(task open in the app) — I'll ping you when it ends"
                    % (esc(self.title), elapsed))
        return "🧠 <b>«%s»</b> · %ds\nthinking…" % (esc(self.title), elapsed)

    def _edit_live_box(self):
        self.last_edit = self.now()
        # PERMANENT CoT boxes: what no longer fits in the live tail is
        # sent as a fixed message (scrolleable from start to end)
        self._cot_flush(COT_TAIL)
        elapsed = int(self.now() - self.t0)
        # F7c: the current tool, next to the CoT — the command being
        # typed live or 🔵 running
        body = self._live_box_body(elapsed) + self._live_tool_line()
        # live_id (not msg_id): after the MOVE of the first flush the live
        # box is the bottom one — editing the old one left the new one
        # stuck at «pensando…» for the rest of the turn (audit 20/10)
        self.live_edit(self.cfg, self.live_id, body)
        if self.steps:
            if self.ph_steps is None:
                self.ph_steps = self.live_send(
                    self.cfg, "🔧 <b>«%s»</b> — steps:" % esc(self.title))
            if self.ph_steps:
                last = self.steps[-12:]
                self.live_edit(self.cfg, self.ph_steps,
                               "🔧 <b>«%s»</b> — %d step%s:\n%s"
                               % (esc(self.title), len(self.steps),
                                  "s" if len(self.steps) != 1 else "",
                                  "\n".join(
                                      "• %s%s" % (esc(n),
                                                  " · " + esc(d) if d else "")
                                      for n, d in last)
                                  + ("\n···" if len(self.steps) > 12 else "")))

    # ------------------------------------------------- main loop

    def run(self):
        while self.now() < self.deadline:
            if not self.th.is_alive():
                break
            if self.stop_requests < 1 and self.stop_requested(self.sid):
                # /stop: stop with the worker's OWN runtime (a real
                # stop). The turn ends shortly after and th cuts the loop.
                self.stop_requests += 1
                self.pwlog("stop marker: session/stop %s" % self.sid)
                try:
                    self.c.request("session/stop", {"sessionId": self.sid},
                                   timeout=30)
                except Exception:  # noqa: BLE001
                    pass
            if not self.background \
                    and not self.task_current_ok(self.cfg, self.sid):
            # F15: silence the stream (CoT/boxes/steps) with ONE farewell
            # edit; the turn goes on and the FINAL REPLY still arrives
            # (prompt_worker) — it is a reply to the user's message
                self.background = True
                self.pwlog("live paused: %s is no longer the current task"
                           % self.sid)
                try:
                    self.live_edit(self.cfg, self.msg_id,
                                   "⏸️ <b>«%s»</b> — moved to the background "
                                   "(you switched tasks); I'll ping you when "
                                   "it ends" % esc(self.title))
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._poll_events()
            except Exception:  # noqa: BLE001 - the live view never breaks the turn
                pass
            # steps: every STEP of the turn is an assistant message that
            # completes — lightweight poll of new history every 6 s
            if self.now() - self.last_poll_steps >= 6.0:
                self._poll_steps()
            if self.live_id and not self.background \
                    and self.now() - self.last_edit >= 3.0:
                self._edit_live_box()
            self.sleep(self.cad)  # adaptive: 2 s normal, 5 s large frame
        self._close()
        # [1] is the TAIL of the text (8KB) — the full text comes from the history
        return self.cot_full, self.text, self.steps, self.live_id

    def _close(self):
        """R10 — the close of the turn (the 32 lines that used to close
        run, the densest part and the least directly tested): the final
        shower of text boxes, the CoT button INDEX (or the F45 «sin  # noqa: spanish — the notice's original name, quoted
        CoT» notice) and the pwlog telemetry."""
        # end of the turn: the remaining CoT also goes to permanent
        # messages — the chat keeps the COMPLETE reasoning scrolleable
        if not self.background:  # F15: backgrounded = no closing shower
            try:
                self._cot_flush(0, pace=False)
            except Exception:  # noqa: BLE001
                pass
            if self.cfg.get("cot_buttons"):
                # F43: the tail of the in-flight round gets sealed and THAT
                # round closes with its message and its buttons, like all
                # the others.
                # F49: the turn does NOT leave a global index at the end —
                # before, the last round's message was edited with ALL the
                # turn's buttons (Damian: «en la última box se  # noqa: spanish — Damian's quoted remark
                # acumularon todas, está andando re mal»). Only if some box  # noqa: spanish — Damian's quoted remark (continuation)
                # was left without a message (failed send) is ONE message
                # of latecomers born with THOSE: no button is lost,
                # nothing piles up.
                self._cot_run_close()
                if self.cot_boxes:
                    missing = [i + 1 for i, p in enumerate(self.cot_phs) if not p]
                    if missing:
                        kb = {"inline_keyboard": cotmod.index_buttons(
                            self.sid, int(self.t0), self.cot_labels, missing)}
                        nph = cotmod.ph_of(self.send(
                            self.cfg, cotmod.late_text(
                                self.title, len(missing), t0=self.t0),
                            loud=False, markup=kb))
                        if nph:
                            self.cot_nmsg += 1
                            for i in missing:
                                self.cot_phs[i - 1] = nph
                            cotmod.kb_log("phone late", nph, kb)
                    self._cot_save(done=True)
                elif self._cot_empty():
                    # F45: the stream ran whole and not a single
                    # reasoning_delta — one line to say so (before:
                    # silence = bug?)
                    self.send(self.cfg, cotmod.empty_text(self.title,
                                                          int(self.t0)),
                              loud=False)
        self.pwlog("live fin: deltas=%d reason=%d text=%d steps=%d cot_msgs=%d "
                   "boxes=%d"
                   % (self.n_deltas, self.reason_n, self.text_n,
                      len(self.steps), self.cot_nmsg, len(self.cot_boxes)))
