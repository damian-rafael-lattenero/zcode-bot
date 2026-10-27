"""agent_live — the inside of each subagent, in boxes (F63).

Damian, 24/10: «también quiero ver el trabajo en vivo de cada agente  # noqa: spanish — Damian's quoted request
así del mismo modo que veo los cots, todo condensado en cajitas  # noqa: spanish — continuation
"Progress agent 1.1"... adentro... lo que haya hecho, sea texto del  # noqa: spanish — continuation
agente más pedazo de cot más algún comando ejecutado... no quiero  # noqa: spanish — continuation
que sea un misterio». The parent's stream never shows the agent's  # noqa: spanish — Damian's quoted request (closing line)
inside (ONE Task tool call, blue and mute for minutes) — but the
runtime persists ALL of it: each subagent gets its OWN rollout
`model-io-sess_subagent_agent_*.jsonl`, one record per completed
request, with response.reasoningText (the agent's CoT — main turns
drop thinking from the stored request, subagent responses CARRY it),
response.text (its narration) and response.toolCalls (what it ran).

Design (Damian's answers, 24/10): boxes sealed BY SIZE like the CoT
(the agent's activity stream feeds a CotSealer); the old silent 🔵
Task box FUSES into this (the agent is born as a live message at
SPAWN — the worker/spectator hand the label over via the spawn file —
and each sealed box freezes its OWN message, F57 model); verbosity
mirrors the CoT (summary: no boxes, one done line at close; verbose:
boxes born OPEN via box_open_view); v1 heartbeat + boxes per completed
request (the mid-request resume spike stays v2).

ONE detached watcher per turn (`--agent-live`), spawned by the hook
(Mac turns) and by prompt_worker (phone turns): watches the rollout
DIR for agent files born this turn + the spawn file for labels; ends
when the session leaves running_turns() (covers app AND headless
turns) or at the 35 min cap. Single-flight: pid-file, like spectator.
Imported LAZILY by nothing at module level except live_view/spectator
(note_spawn) — this module must not import spectator at top level
(it would close the hooks→workers→live_view cycle).
"""
import json
import os
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from zcode_bot import config
from zcode_bot import cli_logs as zc_logs
from zcode_bot import live_io  # F73: the fork's live-CoT sidecar
from zcode_bot import reasoning
from zcode_bot import verbosity as vb
from zcode_bot.core import running_turns, _task_current_ok
from zcode_bot.messages import _tool_box
from zcode_bot import limits
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.logging import pwlog

WATCH_CAP_S = 35 * 60   # (own name: the anti-dup gate)
WATCH_S = 2.0          # loop cadence when fswatch is instant/absent
START_GRACE_S = 90     # the hook fires before turn.started lands
HEARTBEAT_S = 90       # "still on it" edit cadence of an agent's live line
BORN_SLACK_MS = 15000  # agent files: first record within this of t0 counts
REASON_KEEP = 2400     # monster reasoningText: head+tail rendered, not all
LINK_SCAN_CAP = 80000  # chars of the first request scanned for the cwd
REASON_HEAD = 200
TEXT_KEEP = 800

# the tools a PARENT uses to spawn an agent (the label handoff listens
# for these names on both surfaces)
AGENT_TOOLS = ("Task", "Agent")

# the REAL name (verified 25/10 on the eri turn): sess_subagent_agent_<uuid>
# — underscore, not hyphen; any subagent KIND matches on the common stem
AGENT_FILE_PREFIX = "model-io-sess_subagent_"

# F65c — background agents OUTLIVE the parent turn (26/10, eri: the turn
# ended 16 s after spawning two pure-reasoning agents, which ran on for
# minutes while every watcher had already cut them as dead): the watcher
# LINGERS past the turn while the runtime still runs agents of this session
LINGER_MAX_S = 2 * 3600   # absolute watcher life (a log that never closes)
STATES_EVERY_S = 10.0     # cadence of the subagent_states log replay
PID_FRESH_S = 10.0        # watcher_up trusts a pid-file touched this recently


def spawn_log_path(sid: str) -> str:
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in sid)
    return os.path.join(config.STATE_DIR, "agent-spawn-%s.jsonl" % safe)


def agent_cache_path(sid: str, t0: float, n: int) -> str:
    """The n-th agent of the turn (sid, t0): SAME shape as a CoT cache
    (boxes/labels/phs/perbox) so the taps, parts and the F59 in-place
    expansion all reuse the CoT machinery."""
    return os.path.join(config.STATE_DIR,
                        "agent-%s-%d-%d.json" % (sid, int(t0), n))


def index_path(sid: str) -> str:
    """The turn's AGENT INDEX (F64): one entry per agent — n, label,
    prompt, state, req_n, boxes, t0 — rewritten by the watcher on every
    change. /subagents and /status read it; the agent caches hold the
    boxes themselves."""
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in sid)
    return os.path.join(config.STATE_DIR, "agents-%s.json" % safe)


def read_index(sid: str) -> Dict[str, Any]:
    try:
        with open(index_path(sid)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def note_spawn(sid: str, name: str, inp: Dict[str, Any]) -> None:
    """The label handoff (called by live_view and the spectator at the
    parent's Task/Agent tool_call): appends {ts, label, prompt} — the
    watcher births the agent's live message from this, instantly, with
    a real label instead of waiting for the first request to complete.
    Never raises (it is called from inside the event loops)."""
    try:
        d = inp if isinstance(inp, dict) else {}
        label = str(d.get("description") or d.get("agent_type")
                    or d.get("subagent_type") or name)
        prompt = str(d.get("prompt") or "")[:200]
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(spawn_log_path(sid), "a") as f:
            f.write(json.dumps({"ts": time.time(), "label": label[:80],
                                "prompt": prompt}) + "\n")
    except OSError:
        pass  # silent-by-design: cache write, best-effort


# ------------------------------------------------------------- rendering

def _tool_line(name: str, inp: Dict[str, Any]) -> str:
    """One light line per command the agent ran (the heavy _tool_box is
    for the parent's own tools; inside a box this is a breadcrumb)."""
    d = inp if isinstance(inp, dict) else {}
    for k in ("command", "path", "url", "query", "pattern", "prompt",
              "description"):
        if d.get(k):
            return "%s %s" % (name, str(d[k])[:90])
    return name


def render_record(rec: Dict[str, Any]) -> str:
    """One completed agent request → the activity chunk that feeds the
    sealer: its reasoning (a monster gets head+tail — the boxes are the
    activity feed, not the full transcript), its narration, its tools."""
    resp = rec.get("response") or {}
    out: List[str] = []
    reason = str(resp.get("reasoningText") or "")
    if reason.strip():
        if len(reason) > REASON_KEEP + REASON_HEAD:
            reason = (reason[:REASON_HEAD]
                      + "\n… [+%d chars of reasoning] …\n"
                      % (len(reason) - REASON_KEEP - REASON_HEAD)
                      + reason[-REASON_KEEP:])
        out.append(reason.strip())
    text = str(resp.get("text") or "").strip()
    if text:
        out.append("💬 " + (text[:TEXT_KEEP] if len(text) > TEXT_KEEP
                            else text))
    for tc in resp.get("toolCalls") or []:
        if isinstance(tc, dict):
            out.append("🔧 " + _tool_line(str(tc.get("name") or "tool"),
                                          tc.get("input") or {}))
    return "\n".join(out)


# ------------------------------------------------------------- the watcher

class _Agent:
    """One spawned agent's live state (pure bookkeeping; the transport
    lives in the loop)."""

    def __init__(self, n: int, label: str, prompt: str,
                 t0: int = 0, born: float = 0.0) -> None:
        self.n = n
        self.label = label
        self.prompt = prompt
        self.t0 = int(t0)            # F65c: the agent's OWN turn (frozen at
        self.born = born or time.time()   # birth — a refresh of the watcher's
        self.aid = ""                # t0 must not move its cache identity)
        self.path: Optional[str] = None    # rollout file once assigned
        self.pos = 0
        self.req_n = 0
        self.sealer = reasoning.CotSealer()
        self.boxes: List[str] = []
        self.labels: List[str] = []
        self.phs: List[Optional[int]] = []
        self.live_ph: Optional[int] = None
        self.last_beat = 0.0
        self.done = False
        self.done_at = 0.0          # F70: when it ended (the index prunes)
        self.state = "running"      # running | done | <cut reason>
        self.last_line = ""         # the latest activity (index/status)

    def cache_kwargs(self) -> Dict[str, Any]:
        return {"boxes": self.boxes, "labels": self.labels,
                "phs": list(self.phs), "perbox": True}


def _read_new(p, pos):
    """spectator._read_new — lazy (a top-level import here would close
    the hooks→workers→live_view→agent_live→spectator cycle)."""
    from zcode_bot.spectator import _read_new as _rn
    return _rn(p, pos)


def _ts_ms(ts: Any) -> Optional[float]:
    """startedAt → epoch ms (the rollout's ISO-with-Z string — spectator
    _ts_ms's lesson, kept local to avoid the import cycle)."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        import datetime
        try:
            return datetime.datetime.fromisoformat(
                ts.replace("Z", "+00:00")).timestamp() * 1000.0
        except ValueError:
            return None
    return None


def _first_record(path: str) -> Optional[Dict[str, Any]]:
    """The file's FIRST record, digested: {ms, prompt, raw} — startedAt
    (epoch ms), the agent's task prompt (the last user message of the
    request: the parent's Task input verbatim) and the request text
    (it carries the parent's cwd — the ONLY parent linkage the runtime
    leaves; verified 25/10 on eri: 7 agents, all with the workspace in
    sdkMessages). None if empty/unreadable (born, no request done)."""
    try:
        with open(path, errors="replace") as f:
            ln = f.readline()
        if not ln.strip():
            return None
        d = json.loads(ln)
    except (OSError, ValueError):
        return None
    req = d.get("request") or {}
    sdk = req.get("sdkMessages") or []
    prompt = ""
    for m in reversed(sdk if isinstance(sdk, list) else []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(x.get("text", x)) if isinstance(x, dict)
                             else str(x) for x in c)
            prompt = str(c or "")
            break
    raw = json.dumps(sdk)[:LINK_SCAN_CAP] if sdk \
        else json.dumps(req.get("body") or "")[:LINK_SCAN_CAP]
    return {"ms": _ts_ms(d.get("startedAt")), "prompt": prompt, "raw": raw}


def _dir() -> str:
    return os.path.dirname(config.rollout_path("x"))


def _agent_files() -> List[str]:
    try:
        names = os.listdir(_dir())
    except OSError:
        return []
    return [os.path.join(_dir(), n) for n in sorted(names)
            if n.startswith(AGENT_FILE_PREFIX)]


def _aid_of(path: str) -> str:
    """F65c — the rollout name IS the agentId: model-io-sess_subagent_
    agent_<uuid>.jsonl → agent_<uuid> (verified 26/10: the CLI log's
    context.agentId of the eri agents matches their rollout names)."""
    name = os.path.basename(path)
    return name[len(AGENT_FILE_PREFIX):-len(".jsonl")]


AGENT_MAX_LIFE_S = 12 * 3600   # running with no end for this long = lost


def agent_session_of(aid: str) -> str:
    """agentId → the subagent's OWN session id (sess_subagent_<aid>)."""
    return "sess_subagent_" + aid


def agent_request_activity(aid: str, now=None, iter_ev=None,
                           max_age: float = 3600.0) -> Dict[str, Any]:
    """F70 — a RUNNING subagent's life signs, replayed from its OWN CLI
    log events (model.* under sess_subagent_<aid>, include_subagents):
    the request in flight (started, iteration, messageCount) and the
    network retries since it started. The rollout is silent mid-request
    (verified 26/10) but the log shows the request breathing — the pipe
    CIRCULATING, Damian's base requirement (26/10)."""
    it = iter_ev or zc_logs.iter_events
    t = now if now is not None else time.time()
    started_at: Optional[float] = None
    iteration = 0
    msg_count = 0
    net_fails = 0
    try:
        events = it(config.BASE, needles=("model.",),
                    sids={agent_session_of(aid)},
                    since_epoch=t - max_age, last_files=2,
                    tail_bytes=0, include_subagents=True)
    except OSError:
        return {}
    for ts, _sid, ev, d in events:
        ctx = d.get("context") or {}
        if ev == "model.request.started":
            started_at, iteration, msg_count = ts, \
                int(ctx.get("iteration") or 0), int(ctx.get("messageCount") or 0)
            net_fails = 0              # counters live per request
        elif ev == "model.request.completed":
            started_at = None          # between requests: nothing in flight
        elif ev == "model.network.failed":
            net_fails += 1
    if started_at is None:
        return {"in_flight": False}
    return {"in_flight": True, "started": started_at,
            "iteration": iteration, "msg_count": msg_count,
            "net_fails": net_fails}


def last_event_age(now=None, iter_ev=None) -> Optional[float]:
    """F70 — the PIPE stamp: how long ago the runtime last wrote an
    event (model.*/turn.*, any session — a subagent's request counts:
    it is the same pipe). None = nothing recent in the log."""
    it = iter_ev or zc_logs.iter_events
    t = now if now is not None else time.time()
    last: Optional[float] = None
    try:
        events = it(config.BASE, needles=("model.", "turn."),
                    sids=None, since_epoch=t - 600, last_files=1,
                    include_subagents=True)
        for ts, _sid, _ev, _d in events:
            last = ts if last is None else max(last, ts)
    except OSError:
        return None
    return (t - last) if last is not None else None


def subagent_states(sid: str, max_age: float = 48 * 3600.0,
                    iter_ev=None, now=None) -> Dict[str, Dict[str, Any]]:
    """F65c — the runtime's OWN truth about a session's subagents,
    replayed from the CLI log (the running_turns pattern): per agentId,
    its latest state. Vocabulary (verified 25-26/10): subagent.spawned
    (foreground) and subagent.background.started → running; .completed →
    done; .stopped → stopped; .failed → failed (context.errorMessage).
    An agent is EITHER foreground or background, never both — last event
    wins. The notification.enqueued noise is skipped by the mapping.
    F69: the window covers BOTH log files (a 2 h cutoff forgot agents
    still running after 2 h — 26/10, eri's 07:36 topologists at 09:50);
    a running agent with no end for AGENT_MAX_LIFE_S reads as «lost»
    (the app was killed: no terminal event will ever come)."""
    it = iter_ev or zc_logs.iter_events
    t = now if now is not None else time.time()
    out: Dict[str, Dict[str, Any]] = {}
    try:
        events = it(config.BASE, needles=("subagent.",), sids={sid},
                    since_epoch=t - max_age,
                    last_files=2, tail_bytes=0)
    except OSError:
        return out
    for ts, _sid, ev, d in events:
        ctx = d.get("context") or {}
        aid = str(ctx.get("agentId") or "")
        if not aid:
            continue
        if ev in ("subagent.spawned", "subagent.background.started"):
            state = "running"
        elif ev.endswith(".completed"):
            state = "done"
        elif ev.endswith(".stopped"):
            state = "stopped"
        elif ev.endswith(".failed"):
            state = "failed"
        else:
            continue
        out[aid] = {"state": state, "bg": "background" in ev,
                    "ts": ts, "error": str(ctx.get("errorMessage") or "")}
    for s in out.values():
        if s["state"] == "running" and t - float(s["ts"]) > AGENT_MAX_LIFE_S:
            s["state"] = "lost"
            s["error"] = "no end in the log for %d h (the app was likely killed)" \
                % int(AGENT_MAX_LIFE_S / 3600)
    return out


class TurnAgents:
    """The watcher's brain: agents of ONE turn, transport-injectable
    (send/edit/delete/spawn-file reads) — the loop feeds it."""

    def __init__(self, cfg: Dict[str, Any], sid: str, title: str,
                 t0: float, deps: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = cfg
        self.sid = sid
        self.title = title
        self.ws = (deps or {}).get("ws", "")  # the parent's workspace (linkage)
        self.t0 = int(t0)
        self.t0_ms = t0 * 1000.0
        self.agents: List[_Agent] = []
        self._spawn_pos = 0
        self._files_seen: set = set()
        self._read_spawn = None
        d = deps or {}
        # F65c — the log's word on this session's agents (alive per the
        # runtime, not per time windows); injectable for tests
        self.states: Dict[str, Dict[str, Any]] = {}
        self._states_at = -1e9
        self.states_fn = d.get("states", lambda: subagent_states(self.sid))
        # F70 — the agent's own model.* life signs + the pipe stamp
        self.activity_fn = d.get("activity", agent_request_activity)
        self.pipe_age_fn = d.get("pipe_age", last_event_age)
        # F73 — the fork's live-CoT sidecar of the agent's OWN session
        # (real chars in flight + the tail it is thinking); None without
        # the fork build — the heartbeat degrades to F70's counters
        self.live_fn = d.get(
            "live", lambda aid: live_io.latest(agent_session_of(aid)))
        self.send = d.get("send", uhttp.send)
        self.edit = d.get("edit", uhttp._live_edit)
        self.delete = d.get("delete", uhttp._delete)
        self.read_new = d.get("read_new", _read_new)
        self.now = d.get("now", time.time)
        self.sleep = d.get("sleep", time.sleep)
        self._sent_n = 0
        self.summary = False   # set by watch() from the verbosity level
        # F64b — the F15 anti-noise rule, read LIVE on every push: the
        # status lines reach the chat only while this session is the
        # bot's active task (a /task switch cuts/starts them mid-turn);
        # the index and the caches are ALWAYS written — /subagents can
        # show any session's agents, the Mac's included.
        # F67 — and NOT while one of its agents is on VIEW: picking a  # noqa: spanish — Damian's quoted request
        # subagent means ONLY that subagent streams (Damian, 26/10:  # noqa: spanish — continuation
        # «si bien elijo una task o un subagente solo me muestre ese  # noqa: spanish — continuation
        # stream» — the parent's lines resume when the view closes.  # noqa: spanish — continuation
        self.may_push = d.get("may_push",
                              lambda: _task_current_ok(self.cfg, self.sid)
                              and not view_alive(self.sid))

    # ------------------------------------------------- log truth (F65c)
    def refresh_states(self) -> None:
        """The runtime's word on this session's agents, throttled (the
        replay scans the CLI log; the loop asks every round)."""
        if self.now() - self._states_at < STATES_EVERY_S:
            return
        self._states_at = self.now()
        try:
            st = self.states_fn()
        except Exception:  # noqa: BLE001 — silent-by-design: advisory probe, degrades to no states
            return
        if isinstance(st, dict):
            self.states = st

    def alive_ids(self, turn_live: bool) -> List[str]:
        """Agents the runtime still runs. Background agents survive their
        parent turn (F65c: they outlive it by minutes); foreground ones
        die with it («spawned» without «completed» is how an interrupt
        looks in the log — waiting on those would ghost-linger forever)."""
        return [aid for aid, s in self.states.items()
                if s.get("state") == "running" and (s.get("bg") or turn_live)]

    def new_turn(self, mt: float) -> None:
        """F65c — a new turn arrived on this session while we live (the
        single-flight held on our fresh pid heartbeat): adopt it — its
        spawns register, its agents number after the ones we carry. The
        agents ALREADY registered keep their own t0 (cache identity)."""
        self.t0 = int(mt)
        self.t0_ms = mt * 1000.0

    def seed(self) -> None:
        """F65c — a fresh watcher inherits the previous index: done rows
        keep their history (and their numbers); cut rows go back to
        PENDING while the log still runs agents of this session (26/10:
        the eri index said «turn ended» for two agents that were alive —
        the lie is undone here). No live line is reborn: the stale one
        stays in the thread and the index carries the truth."""
        idx = read_index(self.sid)
        rows = [r for r in (idx.get("agents") or [])
                if isinstance(r, dict) and r.get("label")][-12:]
        if not rows:
            return
        self._states_at = -1e9
        self.refresh_states()
        anyone_alive = bool(self.alive_ids(True))
        for r in rows:
            state = str(r.get("state") or "running")
            a = _Agent(int(r.get("n") or 0) or len(self.agents) + 1,
                       str(r.get("label") or "?"),
                       str(r.get("prompt") or ""),
                       int(r.get("t0") or self.t0), self.now())
            a.req_n = int(r.get("req_n") or 0)
            a.last_line = str(r.get("last") or "")
            a.aid = str(r.get("aid") or "")
            a.state = "done" if state == "done" else "running"
            a.done = state == "done"
            # a cut row resurrects as PENDING only while agents run
            # (its file/label may be one of them); otherwise history
            if state != "done" and not anyone_alive:
                a.done = True
            p = str(r.get("path") or "")
            if a.done and p and os.path.exists(p):
                a.path = p           # history row: never tailed again
            self.agents.append(a)
        self._index_write()

    def reconcile(self) -> None:
        """F65c — the log closes what the file cannot: an agent whose
        rollout never shows a stop record (stopped/failed, or killed)
        ends when the runtime says so, not when a window expires."""
        for a in self.agents:
            if a.done or not a.aid:
                continue
            st = self.states.get(a.aid)
            if not st or st.get("state") == "running":
                continue
            a.done = True
            a.done_at = self.now()
            if st.get("state") == "done":
                self._close(a, a.last_line)   # the file's stop record
                continue                      # normally got here first
            a.state = str(st.get("state"))
            a.last_line = st.get("error") or a.last_line or a.state
            self._push_edit(a.live_ph, self._status_text(a, a.state))
            self._save(a, done=True)
            self._index_write()

    def _register(self, label: str, prompt: str,
                  path: Optional[str] = None, aid: str = "",
                  born: float = 0.0) -> _Agent:
        a = _Agent(len(self.agents) + 1, label, prompt, self.t0,
                   born or self.now())
        a.path, a.aid = path, aid
        self.agents.append(a)
        return a

    def _push_send(self, text: str):
        if not self.may_push():
            return None
        return reasoning.ph_of(self.send(self.cfg, text, loud=False))

    def _push_edit(self, ph: Optional[int], text: str) -> None:
        if ph is None or not self.may_push():
            return
        self.edit(self.cfg, ph, text)

    # -------------------------------------------------- the task thread
    def _status_text(self, a: _Agent, state: str) -> str:
        """The ONE line an agent shows in the task thread (F64, Damian:
        «en la task solo se mostraría algo como subagente corriendo»):  # noqa: spanish — Damian's quoted request
        running → done/cut. The inside lives behind /subagents."""
        head = "🤖 <b>Agent %d</b> · %s" % (a.n, esc(a.label))
        if state == "running":
            return head + " — running… (req %d · %s) · /subagents" % (
                a.req_n, esc((a.last_line or "…")[:60]))
        if state == "done":
            return "✅ " + head[2:] + " — done · /subagents"
        return "⏹ " + head[2:] + " — cut (%s)" % esc(state)

    def _birth(self, a: _Agent) -> None:
        """The status line shows at EVERY level (F64: with the thread
        already clean, this line IS the «subagent running» notice —
        summary only drops the inside, which lives behind /subagents)."""
        a.live_ph = self._push_send(
            self._status_text(a, "running") + "\n" + esc(a.prompt[:160]))

    def _index_write(self) -> None:
        """The /subagents + /status source: rewritten on every change."""
        try:
            tmp = "%s.tmp.%d" % (index_path(self.sid), os.getpid())
            with open(tmp, "w") as f:
                json.dump({"sid": self.sid, "t0": self.t0, "title": self.title,
                           "ws": self.ws, "updated": self.now(),
                           "agents": [{"n": a.n, "label": a.label,
                                       "prompt": a.prompt[:160],
                                       "state": a.state, "req_n": a.req_n,
                                       "t0": a.t0, "aid": a.aid,
                                       "done_at": a.done_at,
                                       "path": a.path or "",
                                       "last": a.last_line}
                                      for a in self.agents]}, f)
            os.replace(tmp, index_path(self.sid))
        except OSError:
            pass  # silent-by-design: cache write, best-effort

    # -------------------------------------------------- label handoff
    def poll_spawns(self) -> None:
        """New spawn-file entries of THIS turn → register + birth the
        status line (the spawn arrives instantly; the file lags until
        the agent's first request completes)."""
        p = spawn_log_path(self.sid)
        try:
            records, self._spawn_pos = self.read_new(p, self._spawn_pos)
        except OSError:
            return
        for r in records:
            try:
                ts = float(r.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts < self.t0 - 5.0:
                continue
            a = self._register(str(r.get("label") or "?"),
                               str(r.get("prompt") or ""), born=ts)
            self._birth(a)
            self._index_write()

    # -------------------------------------------------- file assignment
    def assign_files(self) -> None:
        """Agent rollout files born this turn → assign FIFO to the
        registered agents (a file without a spawn entry registers an
        implicit agent — attach mid-turn without the label). F65c: a
        file whose agentId the log says ALIVE is ours regardless of age
        — background agents can land their first completed request
        minutes after the turn that spawned them ended."""
        pending = [a for a in self.agents if a.path is None and not a.done]
        for path in _agent_files():
            if path in self._files_seen:
                continue
            meta = _first_record(path)
            if meta is None or meta["ms"] is None:
                continue              # born, no completed request yet
            aid = _aid_of(path)
            alive = self.states.get(aid, {}).get("state") == "running"
            if not alive and meta["ms"] < self.t0_ms - BORN_SLACK_MS:
                self._files_seen.add(path)   # previous turn's agent
                continue
            if not alive and self.ws and self.ws.rstrip("/") not in meta["raw"]:
                self._files_seen.add(path)   # another workspace's agent
                continue
            self._files_seen.add(path)
            # the spawn whose prompt this file carries (parallel spawns
            # of the same turn resolve exactly); FIFO when unknown
            a = None
            for cand in pending:
                if cand.prompt and meta["prompt"].startswith(cand.prompt[:80]):
                    a = cand
                    break
            if a is None and pending:
                a = pending[0]
            if a is not None:
                pending.remove(a)
            else:
                # no spawn entry (attach mid-turn, spectator missing — or
                # an adopted background agent): the agent's own prompt is
                # its label
                a = self._register(meta["prompt"][:60] or "subagent",
                                   meta["prompt"][:160], aid=aid)
                self._birth(a)
            a.path, a.aid = path, aid
            self._index_write()

    # -------------------------------------------------- content
    def poll_agents(self) -> None:
        """New records of each assigned file → seal boxes, freeze their
        messages, keep the live line at the bottom, close on stop."""
        for a in self.agents:
            if a.path is None or a.done:
                continue
            records, a.pos = self.read_new(a.path, a.pos)
            for rec in records:
                self._on_record(a, rec)
            if records:
                a.last_beat = self.now()

    def _on_record(self, a: _Agent, rec: Dict[str, Any]) -> None:
        resp = rec.get("response") or {}
        a.req_n += 1
        final = str(resp.get("finishReason") or "") == "stop"
        if self.summary:
            # summary: no boxes, no cache (nothing to tap) — only the
            # done line carries the agent's result out
            if final:
                a.done = True
                self._close(a, str(resp.get("text") or ""))
            return
        tcs = [t for t in resp.get("toolCalls") or [] if isinstance(t, dict)]
        a.last_line = (_tool_line(str(tcs[-1].get("name") or "tool"),
                                  tcs[-1].get("input") or {})
                       if tcs else (str(resp.get("text") or "")[:90]
                                    or "thinking…"))
        if final:
            a.done = True
            self._close(a, str(resp.get("text") or ""))
        else:
            self._push_edit(a.live_ph, self._status_text(a, "running")
                            + "\n" + esc(a.last_line[:120]))
        self._index_write()

    def _close(self, a: _Agent, final_text: str) -> None:
        a.state = "done"
        a.done_at = a.done_at or self.now()
        a.last_line = final_text[:180]
        if a.live_ph is not None:
            self._push_edit(a.live_ph, self._status_text(a, "done")
                            + "\n" + esc(final_text[:180]))
        else:
            # no status line (quiet so far, or a birth lost to a 429):
            # the RESULT still arrives — if this session may push now
            self._push_send("✅ <b>Agent %d</b> · %s — done: %s"
                            % (a.n, esc(a.label), esc(final_text[:280])))
        self._save(a, done=True)

    def cut_all(self, reason: str) -> None:
        """Turn end: seal the tails and mark the cuts (an agent killed
        mid-flight never writes its stop record)."""
        for a in self.agents:
            if a.done:
                continue
            a.state = reason
            a.done_at = self.now()
            a.last_line = a.last_line or "cut before its first request"
            self._push_edit(a.live_ph, self._status_text(a, reason))
        self._index_write()

    def heartbeat(self) -> None:
        """The long-request heartbeat — F70: with the agent's OWN life
        signs (request in flight, iterations, network failures) and the
        pipe stamp, not a bare clock (the file only grows per completed
        request; a pure-reasoning one runs silent for half an hour).
        F73: with the fork's live-io sidecar the heartbeat also says HOW
        MUCH it is thinking (real chars from the runtime's own
        accumulator) and quotes its last thought — reading, not just
        counting."""
        now = self.now()
        for a in self.agents:
            if a.done or a.live_ph is None:
                continue
            if now - a.last_beat < HEARTBEAT_S:
                continue
            a.last_beat = now
            bits = ["req %d in flight" % (a.req_n + 1)]
            if a.aid:
                lv = self.live_fn(a.aid)   # F73: the live-CoT sidecar
                if lv:
                    c = live_io.chars_line(lv)
                    if c:
                        bits.insert(0, c)
                    lt = [ln for ln in live_io.tail_of(lv, 90).splitlines()
                          if ln.strip()]
                    if lt:
                        bits.append("«%s»" % esc(lt[-1][-70:]))
                act = self.activity_fn(a.aid) or {}
                if act.get("in_flight"):
                    mins = max(1, int((now - float(
                        act.get("started") or now)) / 60))
                    bits.insert(0, "request %d min" % mins)
                    if act.get("iteration"):
                        bits.append("iter %d" % int(act["iteration"]))
                    if act.get("net_fails"):
                        bits.append("net ✗ %d" % int(act["net_fails"]))
            age = self.pipe_age_fn()
            if age is not None:
                bits.append("pipe %ds" % max(0, int(age)))
            self._push_edit(a.live_ph, self._status_text(a, "running")
                            + "\n⏱ " + " · ".join(bits))

    def _save(self, a: _Agent, **extra: Any) -> None:
        try:
            tmp = "%s.tmp.%d" % (agent_cache_path(self.sid, a.t0, a.n),
                                 os.getpid())
            with open(tmp, "w") as f:
                json.dump({"sid": self.sid, "t0": a.t0, "agent": a.n,
                           "title": "%s · agent %d" % (self.title, a.n),
                           **a.cache_kwargs(), **extra}, f)
            os.replace(tmp, agent_cache_path(self.sid, a.t0, a.n))
        except OSError:
            pass  # silent-by-design: cache write, best-effort


def _ag_rows(rows: List[List[Dict[str, Any]]], n: int
             ) -> List[List[Dict[str, Any]]]:
    """box_open_view/show build ct:/cr: rows (CoT surfaces); the agent's
    buttons need the SAME shapes with the ag:/agr: vocabulary (the
    callback carries the agent number as an extra segment)."""
    out = []
    for row in rows:
        new_row = []
        for b in row:
            data = str(b.get("callback_data") or "")
            parts = data.split(":")
            if data.startswith("ct:"):
                # ct:sid:t0:n[:p] → ag:sid:t0:agent:n[:p]
                new_row.append(dict(b, callback_data="ag:%s:%s:%d:%s"
                                    % (parts[1], parts[2], n,
                                       ":".join(parts[3:]))))
            elif data.startswith("cr:"):
                new_row.append(dict(b, callback_data="agr:%s:%s:%d:%s"
                                    % (parts[1], parts[2], n,
                                       ":".join(parts[3:]))))
            else:
                new_row.append(b)
        out.append(new_row)
    return out


# ------------------------------------------------------------- the loop

def watcher_pid_path(sid: str) -> str:
    return os.path.join(config.STATE_DIR, "agent-live-%s.pid" % sid)


def watcher_up(sid: str, proc_alive=None) -> bool:
    """Single-flight: a live watcher with a FRESH pid heartbeat. F65c
    replaced the marker-age rule («a pid older than the marker is a
    previous turn's watcher») with freshness: the watcher now touches
    its pid-file every round because it LINGERS past its turn while
    background agents run — under the old rule the very next turn of a
    session with agents in flight would double-spawn (26/10). A hung
    watcher (no touches in PID_FRESH_S) does NOT block the respawn."""
    try:
        with open(watcher_pid_path(sid)) as f:
            pid = int(f.read().strip() or 0)
        mt = os.path.getmtime(watcher_pid_path(sid))
    except (OSError, ValueError):
        return False
    if pid <= 0 or time.time() - mt > PID_FRESH_S:
        return False
    if proc_alive is None:
        from zcode_bot.workers import _proc_alive  # lazy
        proc_alive = _proc_alive
    return bool(proc_alive(pid))


RESPAWN_EVERY_S = 60.0
_respawn_at = [0.0]   # the poller asks every cycle; the scan runs per minute
DONE_KEEP_S = 600.0   # F70: ended agents stay listed this long, then prune


def respawn_orphans(cfg: Dict[str, Any], spawn=None, states_fn=None,
                    now=None) -> List[str]:
    """F69 — the watcher dies with every deploy (code_changed steps it
    down) and with any crash, while background agents run on for an
    hour (26/10, eri: three topologists orphaned at 08:38, one finished
    at 08:50 unseen). The poller relaunches a watcher for every session
    whose index holds a 🟢 row the log still confirms RUNNING in the
    background and that has no live watcher. Throttled: one scan per
    minute; watcher_up keeps it single-flight. Returns the sids spawned."""
    t = now if now is not None else time.time()
    if t - _respawn_at[0] < RESPAWN_EVERY_S:
        return []
    _respawn_at[0] = t
    if not cfg.get("agent_stream"):
        return []
    try:
        names = os.listdir(config.STATE_DIR)
    except OSError:
        return []
    out: List[str] = []
    for name in sorted(names):
        if not (name.startswith("agents-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(config.STATE_DIR, name)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        sid = str((d or {}).get("sid") or "") if isinstance(d, dict) else ""
        rows = [r for r in (d.get("agents") or []) if isinstance(r, dict)
                and r.get("aid") and (r.get("state") or "running") == "running"] \
            if sid else []
        if not rows or watcher_up(sid):
            continue
        st = (states_fn or (lambda: subagent_states(sid)))()
        if not any(st.get(str(r["aid"]), {}).get("state") == "running"
                   and st.get(str(r["aid"]), {}).get("bg") for r in rows):
            continue
        title = str(d.get("title") or sid[-8:])
        ws = str(d.get("ws") or "")
        if spawn is not None:
            spawn(sid, title, ws)
        else:
            import subprocess
            import sys
            from zcode_bot.hooks import _shim_path  # lazy (hooks → dispatch cycle)
            subprocess.Popen(
                [sys.executable, _shim_path(), "--agent-live", sid, title,
                 str(int(t)), ws], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pwlog("agent-live %s: orphan agents still running — watcher "
              "relaunched" % sid[-8:])
        out.append(sid)
    return out


def watch_agents(cfg: Dict[str, Any], sid: str, title: str, t0: float, ws: str,
          deps: Optional[Dict[str, Any]] = None) -> int:
    """The watcher loop: ONE session, its turns and their agents. The
    turn is alive while running_turns() has it (app and headless turns
    both register there) or the Mac's marker stands — and F65c: past the
    turn, the watcher LINGERS while the log runs background agents of
    this session, adopting each new turn that arrives (the t0 refresh).
    Returns the boxes sent."""
    from zcode_bot import fswatch
    d = deps or {}
    now = d.get("now", time.time)
    running = d.get("running", running_turns)
    wait = d.get("wait", fswatch.wait_change)
    pid = watcher_pid_path(sid)
    try:                                    # race guard: a hook and a worker
        with open(pid) as f:                 # can spawn us for the same turn
            other = int(f.read().strip() or 0)
        if other and other != os.getpid() and time.time() - \
                os.path.getmtime(pid) <= PID_FRESH_S:
            return 0                         # the live one owns the session
    except (OSError, ValueError):
        pass  # silent-by-design: liveness probe, the except is the verdict
    ta = TurnAgents(cfg, sid, title, t0, deps=dict(d, ws=ws))
    ta.summary = vb.level_of(cfg) == "summary"
    ta.seed()
    os.makedirs(config.STATE_DIR, exist_ok=True)

    def touch() -> None:
        # F65c heartbeat: watcher_up trusts a FRESH pid-file — a lingering
        # watcher must still block a second spawn on the next turn
        try:
            os.utime(pid, None)
        except OSError:
            pass  # silent-by-design: beat touch, best-effort

    with open(pid, "w") as f:
        f.write(str(os.getpid()))
    deadline = now() + WATCH_CAP_S
    hard_end = now() + LINGER_MAX_S
    paths = [_dir(), spawn_log_path(sid)]
    boxes = 0
    started = now()
    tok = reasoning._marker_state(sid)
    why = "turn ended"   # cut reason; the while-else swaps it at the cap
    try:
        while now() < hard_end and now() < deadline:
            touch()
            if config.code_changed():
                pwlog("agent-live %s: new code deployed — stepping down"
                      % sid[-8:])
                return boxes
            ta.refresh_states()
            # the turn is alive while the CLI log says so OR the Mac's
            # marker stands; and never during the startup grace — the
            # hook fires BEFORE turn.started lands in the log (25/10:
            # watchers dying in 1s with agents=0)
            turn_live = (now() - started <= START_GRACE_S
                         or sid in running()
                         or os.path.exists(config.marker_path(sid)))
            if not turn_live:
                if not ta.alive_ids(False):
                    break
                # F65c linger: background agents outlive the turn — 26/10
                # cut two healthy agents as «before its first request»
                deadline = now() + WATCH_CAP_S
            else:
                st = reasoning._marker_state(sid)
                if st is not None and st != tok and st[1]:
                    tok = st   # a NEW turn on this session: adopt it
                    ta.new_turn(st[0])
            ta.poll_spawns()
            ta.assign_files()
            ta.poll_agents()
            ta.reconcile()
            ta.heartbeat()
            boxes = sum(len(a.boxes) for a in ta.agents)
            wait(paths, timeout=WATCH_S)
        else:
            why = "watcher cap"
        ta.cut_all(why)
        boxes = sum(len(a.boxes) for a in ta.agents)
        pwlog("agent-live end: agents=%d boxes=%d"
              % (len(ta.agents), boxes))
        return boxes
    finally:
        try:
            os.remove(watcher_pid_path(sid))
        except OSError:
            pass  # silent-by-design: cleanup, best-effort


def cmd_agent_live(sid: str, title: str, t0: str, ws: str) -> None:
    """Entrypoint of the detached subprocess (gateway --agent-live)."""
    from zcode_bot.utils.config import load_config
    from zcode_bot.utils.logging import bind_context
    bind_context(sid=sid[-8:], turn=t0, component="agent-live")
    try:
        watch_agents(load_config(), sid, title, float(t0), ws)
    except Exception as exc:  # noqa: BLE001 — the subprocess reports, not dies
        pwlog("agent-live %s failed: %s\n%s"
             % (sid[-8:], exc, traceback.format_exc()))


# ------------------------------------------------------------- /subagents (F64)

def _agent_of(sid: str, t0: int, n: int) -> Optional[Dict[str, Any]]:
    try:
        with open(agent_cache_path(sid, t0, n)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


OTHERS_FRESH_S = 1800   # other sessions' indexes listed while this fresh


def indexes_live(exclude_sid: Optional[str] = None,
                 now=None) -> List[Dict[str, Any]]:
    """F64b — every OTHER session with agents worth listing: a running
    agent, or an index updated in the last OTHERS_FRESH_S (the Mac's
    turns included — their watcher indexes even when it may not push)."""
    try:
        names = os.listdir(config.STATE_DIR)
    except OSError:
        return []
    t = now if now is not None else time.time()
    out = []
    for name in sorted(names):
        if not (name.startswith("agents-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(config.STATE_DIR, name)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not d.get("agents"):
            continue
        if exclude_sid and d.get("sid") == exclude_sid:
            continue
        if any((a.get("state") or "running") == "running" and a.get("aid")
               for a in d["agents"] if isinstance(a, dict)):
            d = reconciled_index(str(d.get("sid") or ""))   # F69: log truth
        live = any((a.get("state") or "running") == "running"
                   for a in d["agents"])
        if live or t - float(d.get("updated") or 0) <= OTHERS_FRESH_S:
            out.append(d)
    return out


def _agent_rows(d: Dict[str, Any]) -> Tuple[List[str], List[List[Dict[str, str]]]]:
    sid, t0 = str(d.get("sid") or ""), int(d.get("t0") or 0)
    lines, kb = [], []
    for a in d.get("agents") or []:
        st = a.get("state") or "running"
        mark = "🟢" if st == "running" else ("✅" if st == "done" else "⏹")
        nb = int(a.get("boxes") or 0)
        lines.append("%s <b>%d.</b> %s — %s · req %d · %d box%s"
                     % (mark, a["n"], esc(a.get("label") or "?"), st,
                        int(a.get("req_n") or 0), nb, "es" if nb != 1 else ""))
        # F65c: each row carries its OWN turn's t0 (a lingering watcher
        # refreshes its t0 per turn — an agent's cache identity is frozen)
        kb.append([{"text": ("%s %d · %s" % (mark, a["n"],
                                              a.get("label") or "?"))[:36],
                    "callback_data": "sa:%s:%d:%d"
                    % (sid, int(a.get("t0") or t0), a["n"])}])
    return lines, kb


def reconciled_index(sid: str, states_fn=None) -> Dict[str, Any]:
    """F69 — the index as the runtime sees it NOW. A watcher that died
    (every deploy's code_changed steps it down; a crash) leaves its 🟢
    rows frozen: 26/10, eri — agent 1 finished 12 min AFTER its watcher
    stepped down and /subagents kept showing it running for an hour. The
    log's terminal word corrects the row (and is PERSISTED, so every
    surface — list, /status, /task nesting, the view's aid — agrees).
    F70 — and ended rows PRUNE after DONE_KEEP_S (Damian: «la lista se  # noqa: spanish — Damian's quoted request
    limpie... sino va a tener muchísimos botones con subagentes que ya  # noqa: spanish — continuation
    terminaron su ejecución»): ~10 min to read the result, then the  # noqa: spanish — continuation
    list cleans itself."""
    idx = read_index(sid)
    rows = [r for r in (idx.get("agents") or []) if isinstance(r, dict)]
    if not rows:
        return idx
    changed = False
    if any((r.get("state") or "running") == "running" and r.get("aid")
           for r in rows):
        st = (states_fn or (lambda: subagent_states(sid)))()
        for r in rows:
            s = st.get(str(r.get("aid") or ""))
            if s and s.get("state") != "running" \
                    and (r.get("state") or "running") == "running":
                r["state"] = str(s["state"])
                if s.get("error"):
                    r["last"] = str(s["error"])
                r["done_at"] = time.time()
                changed = True
    now = time.time()
    kept = []
    for r in rows:
        if (r.get("state") or "running") == "running":
            kept.append(r)
            continue
        ended = float(r.get("done_at") or 0.0) \
            or float(idx.get("updated") or 0.0)
        if not ended or now - ended <= DONE_KEEP_S:
            kept.append(r)     # recent: still tappable, /subagents n works
    if len(kept) != len(rows):
        changed = True
        idx["agents"] = kept
    if changed:
        idx["updated"] = now
        try:
            tmp = "%s.tmp.%d" % (index_path(sid), os.getpid())
            with open(tmp, "w") as f:
                json.dump(idx, f)
            os.replace(tmp, index_path(sid))
        except OSError:
            pass  # silent-by-design: cache write, best-effort
    return idx


def task_agent_rows(sid: str) -> Tuple[List[str], List[List[Dict[str, str]]]]:
    """F65c — one task's agents for the /task list (Damian, 26/10: «que  # noqa: spanish — Damian's quoted request
    me muestre la lista de tasks y subagentes por tasks también»): its  # noqa: spanish — continuation
    index while worth showing (an agent running, or updated recently),  # noqa: spanish — continuation
    each one tappable — the SAME sa: pick as /subagents."""
    d = reconciled_index(sid) if sid else {}
    if not d.get("agents"):
        return [], []
    live = any((a.get("state") or "running") == "running"
               for a in d["agents"])
    if not live and time.time() - float(d.get("updated") or 0) > OTHERS_FRESH_S:
        return [], []
    return _agent_rows(d)


def subagents_list(sid: str, title: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """/subagents: the agents of the active task's CURRENT turn — one
    button each with its descriptive title (Damian: «que tenga su título  # noqa: spanish — Damian's quoted request
    descriptivo»), THEN the other sessions with agents (F64b: the Mac's  # noqa: spanish — continuation
    turns — Damian: «¿podría ver los subagents de la Mac?»). Pick one →  # noqa: spanish — Damian's quoted request
    its run, shown like a task's; the button carries its session."""
    idx = reconciled_index(sid) if sid else {}
    lines: List[str] = []
    kb: List[List[Dict[str, str]]] = []
    if idx.get("agents"):
        lines.append("🤖 <b>Subagents of «%s»</b> 🎯" % esc(title))
        l2, k2 = _agent_rows(idx)
        lines += l2
        kb += k2
    else:
        lines.append("🤖 No subagents in «%s» yet." % esc(title))
    for d in indexes_live(exclude_sid=sid):
        lines.append("\n🖥 <b>«%s»</b> (other session)" % esc(d.get("title") or "?"))
        l2, k2 = _agent_rows(d)
        lines += l2
        kb += k2
    if not kb:
        return "\n".join(lines) + "\n(the index appears when a task spawns one)", None
    lines.append("\nTap one to see its run:")
    return "\n".join(lines), {"inline_keyboard": kb}


def subagent_show(cfg: Dict[str, Any], sid: str, t0: int, n: int,
                  send=None) -> Optional[str]:
    """The agent's RUN as a task's would look (F64): its status line +
    every box as its own message with its «Agent N.C» button (born OPEN
    in verbose), the phs persisted into the agent cache so the in-place
    expansion (ag:/agr:) owns these very messages. Boxes already
    materialized (ph set) are not re-sent: a second look shows only the
    new ones."""
    send = send or uhttp.send
    d = _agent_of(sid, t0, n)
    idx = read_index(sid)
    meta: Dict[str, Any] = next(
        (a for a in (idx.get("agents") or []) if a.get("n") == n), {})
    label = meta.get("label") or (d or {}).get("title") or "agent %d" % n
    if d is None:
        if meta:
            return ("🤖 <b>Agent %d</b> · %s — %s · req %d · no boxes yet\n%s"
                    % (n, esc(label), meta.get("state") or "running",
                       int(meta.get("req_n") or 0), esc(meta.get("last") or "")))
        return "🤔 I have no record of that agent (older turn, or purged)."
    boxes = d.get("boxes") or []
    labels = d.get("labels") or []
    phs = list(d.get("phs") or [None] * len(boxes))
    phs += [None] * (len(boxes) - len(phs))
    st = meta.get("state") or ("done" if d.get("done") else "running")
    mark = "🟢" if st == "running" else ("✅" if st == "done" else "⏹")
    send(cfg, "%s <b>Agent %d</b> · %s — %s · req %d · %d box%s\n%s"
         % (mark, n, esc(label), st, int(meta.get("req_n") or 0), len(boxes),
            "es" if len(boxes) != 1 else "", esc(meta.get("last") or "")),
         loud=False)
    sent = 0
    for i, (box, lab) in enumerate(zip(boxes, labels), 1):
        if phs[i - 1]:
            continue  # already in the chat from an earlier /subagents
        if vb.born_open(cfg):
            text, kb = reasoning.box_open_view(sid, t0, i, lab,
                                               "%s · agent %d" % (label, n), box)
            kb["inline_keyboard"] = _ag_rows(kb["inline_keyboard"], n)
        else:
            text = "🤖 <b>%s</b> · %s" % (esc(lab), reasoning.fmt_tok(len(box)))
            kb = {"inline_keyboard": reasoning.agent_buttons(sid, t0, n, [lab],
                                                             start=i)}
        ph = reasoning.ph_of(send(cfg, text, loud=False, markup=kb))
        phs[i - 1] = ph
        reasoning.kb_log("subagent box %s" % lab, ph, kb)
        sent += 1
        if sent % 3 == 0:
            time.sleep(1.0)
    if sent:
        d["phs"] = phs
        try:
            tmp = "%s.tmp.%d" % (agent_cache_path(sid, t0, n), os.getpid())
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, agent_cache_path(sid, t0, n))
        except OSError:
            pass  # silent-by-design: cache write, best-effort
    if not boxes:
        return None
    return None if sent else ("(the %d box%s above are already in the chat — "
                              "nothing new since)" % (len(boxes),
                                                      "es" if len(boxes) != 1 else ""))


def status_lines(sid: Optional[str]) -> List[str]:
    """/status (F64): what is running INSIDE the active task right now —
    its subagents (from the index) with their latest activity."""
    if not sid:
        return []
    idx = reconciled_index(sid)
    agents = idx.get("agents") or []
    live = [a for a in agents if (a.get("state") or "running") == "running"]
    out: List[str] = []
    if agents:
        out.append("🤖 <b>Subagents</b>: %d running · %d total (/subagents)"
                   % (len(live), len(agents)))
        for a in live[:6]:
            out.append("  • %d. %s — req %d · %s"
                       % (a["n"], esc(a.get("label") or "?"),
                          int(a.get("req_n") or 0),
                          esc((a.get("last") or "…")[:70])))
    others = [(d.get("title") or "?",
               sum(1 for a in d["agents"]
                   if (a.get("state") or "running") == "running"))
              for d in indexes_live(exclude_sid=sid)]
    others = [(t, n) for t, n in others if n]
    if others:
        out.append("🖥 other sessions with agents running: "
                   + " · ".join("«%s» %d" % (esc(t), n) for t, n in others)
                   + " (/subagents)")
    return out


# ------------------------------------------------------- the view (F65)

VIEW_WAIT_S = 2.0
NARRATION_CAP = 1500   # the agent's intermediate narration, per message
VIEW_FRESH_S = 10.0    # the view heartbeats its pid; older = not muting
VIEW_IDLE_CUT_S = 600  # no-aid fallback: this silent + parent gone = cut
VIEW_TAIL_CAP = 400000  # bytes of the file's tail scanned for a stop record
AG_RESUME_TIMEOUT_S = 90   # F70b: resuming the agent's session (first hit)
AG_POLL_TIMEOUT_S = 30      # …and each session/messages poll on it
VIEW_BEAT_BORN_S = 4.0  # F69: the in-flight line is born this soon after the pick
VIEW_BEAT_S = 45.0      # …and re-edited with REAL activity at this cadence


def view_alive(sid: str) -> bool:
    """F67 — a live agent view MUTES its parent session's stream: picking
    a subagent shows ONLY that subagent. Freshness, not existence: the
    view heartbeats its pid-file every round, so a view killed by force
    (no finally, no sweep) stops muting by itself."""
    try:
        return time.time() - os.path.getmtime(view_pid_path(sid)) <= VIEW_FRESH_S
    except OSError:
        return False


def view_pid_path(sid: str) -> str:
    return os.path.join(config.STATE_DIR, "agent-view-%s.pid" % sid)


def stop_views() -> None:
    """F65 — Damian's exit rule, verbatim: «para salir de dentro del  # noqa: spanish — Damian's quoted rule
    subagente es tan simple como tirar el command task y elegir la task  # noqa: spanish — continuation
    padre nuevamente — eso debería dejar de streamear». Any /task pick  # noqa: spanish — continuation
    sweeps the view pid-files; every live view steps down on its next
    round. Picking another agent replaces its parent's view the same
    way (the new pid wins)."""
    try:
        names = os.listdir(config.STATE_DIR)
    except OSError:
        return
    for name in names:
        if name.startswith("agent-view-") and name.endswith(".pid"):
            try:
                os.remove(os.path.join(config.STATE_DIR, name))
            except OSError:
                pass  # silent-by-design: cleanup, best-effort


def _resolve_agent_file(sid: str, n: int) -> Optional[Dict[str, Any]]:
    """The n-th agent's rollout file: the index carries the path (the
    watcher writes it on assignment); older indexes fall back to a scan
    by prompt (the linkage F63c verified). Carries the row's aid (F65c)
    for the log-truth close."""
    idx = read_index(sid)
    meta: Dict[str, Any] = next(
        (a for a in (idx.get("agents") or []) if a.get("n") == n), {})
    aid = str(meta.get("aid") or "")
    if meta.get("path") and os.path.exists(meta["path"]):
        return {"path": meta["path"], "label": meta.get("label") or "agent %d" % n,
                "title": idx.get("title") or sid[-8:], "aid": aid}
    prompt = str(meta.get("prompt") or "")
    if prompt:
        for path in _agent_files():
            m = _first_record(path)
            if m and m["prompt"].startswith(prompt[:80]):
                return {"path": path, "label": meta.get("label") or "agent %d" % n,
                        "title": idx.get("title") or sid[-8:], "aid": aid}
    return None


def _last_record(path: str) -> Optional[Dict[str, Any]]:
    """The file's LAST record (F68: picking a DONE agent answers at once
    instead of tailing a corpse). A record larger than the tail cap is
    unparsable → None → the agent reads as running (safe side)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end == 0:
                return None
            f.seek(max(0, end - VIEW_TAIL_CAP))
            tail = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


class _StepDown(Exception):
    """F67 — the pid-file left (a /task pick, a newer view): stop SENDING
    at once, even mid catch-up burst (26/10: a sweep between rounds let
    a long burst keep landing after the user had picked another task)."""


def view_agent(cfg: Dict[str, Any], sid: str, n: int,
               deps: Optional[Dict[str, Any]] = None) -> int:
    """F65 — picking a subagent IS picking a task (Damian: «el subagente  # noqa: spanish — Damian's quoted request
    hijo es exactamente igual al padre que lo creó, en cuerpo y alma»).  # noqa: spanish — continuation
    One detached view per parent session, in the task's own format —
    🧠 its reasoning in size-sealed CoT boxes with the ag: buttons (born
    open in verbose, F59 in-place on tap), 🔵 a box per tool it runs,
    🖥️ its narration, and its FULL final answer at close, then ↩️ back.
    F68 — LIVE FROM THE PICK, no history replay (Damian, 26/10: «yo no  # noqa: spanish — Damian's quoted request
    lo quiero desde el inicio, lo quiero desde el momento que lo  # noqa: spanish — continuation
    seleccioné» — the F65 whole-run catch-up poured dozens of CoT boxes  # noqa: spanish — continuation
    for a pure-reasoning agent). A DONE agent answers at once. Only the
    chosen one streams; /task sweeps it away (stop_views)."""
    from zcode_bot import fswatch
    d = deps or {}
    send = d.get("send", uhttp.send)
    edit = d.get("edit", uhttp._live_edit)
    wait = d.get("wait", lambda p, timeout: fswatch.wait_change(p, timeout))
    read_new = d.get("read_new", _read_new)
    now = d.get("now", time.time)
    sleep = d.get("sleep", time.sleep)
    running = d.get("running", running_turns)
    states = d.get("states", lambda: subagent_states(sid))
    res = _resolve_agent_file(sid, n)
    if res is None:
        send(cfg, "🤔 That agent's file is gone (older turn — its boxes "
                  "expired with the rotation).")
        return 0
    path, label, title, aid = res["path"], res["label"], res["title"], res["aid"]
    # F70 — the agent's own model.* life signs + the pipe stamp (bound
    # AFTER aid: the default reads its session)
    activity = d.get("activity",
                     lambda: agent_request_activity(aid) if aid else {})
    pipe_age = d.get("pipe_age", last_event_age)
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(view_pid_path(sid), "w") as f:
        f.write(str(os.getpid()))
    t0 = int(now())
    sealer = reasoning.CotSealer()
    boxes: List[str] = []
    labels: List[str] = []
    phs: List[Optional[int]] = []
    pass_n = 0
    sent_n = 0
    final_done = False
    # F70b — the agent's OWN session messages as the LIVE CoT source:
    # resume + session/messages serves sess_subagent_* sessions (verified
    # live 26/10 — my cold call failed only for lacking the resume, the
    # F44 lesson). The ledger is the same MONOTONIC one the Mac watcher
    # uses (F65b: the store REWRITES the step in flight — a shrink is a
    # lie and the ledger absorbs it). The FILE keeps driving
    # tools/narration/final/done; its record-sealer turns OFF the moment
    # the messages path produces its first box (no double sealing), and
    # stays the whole story when the messages path is unavailable.
    led = reasoning.BoxLedger()
    ag_client: List[Any] = []
    ag_dead = not aid
    ag_msgs_fn = d.get("agent_msgs")     # injectable: () → msgs | None
    ag_live = [False]                    # the messages path produced boxes
    title_a = "A%d · %s" % (n, label)

    def _sync_view_lists() -> None:
        """The ledger's lists are REBOUND by sync (replace semantics) —
        splice in place so every closure (bundle, save, close) sees the
        same boxes the ledger sealed."""
        boxes[:] = led.boxes
        labels[:] = led.labels
        phs[:] = list(led.box_phs) + [None] * (len(led.boxes)
                                               - len(led.box_phs))

    def _poll_agent_msgs():
        """The agent session's messages, or None when the path is dead.
        Falls back permanently on the first failure (the file path
        remains)."""
        nonlocal ag_dead
        if ag_dead:
            return None
        try:
            if ag_msgs_fn is not None:
                return ag_msgs_fn() or None
            if not ag_client:
                from zcode_bot import proto as zc_proto
                c = zc_proto.make()
                r = c.request("session/resume",
                              {"sessionId": agent_session_of(aid)},
                              timeout=AG_RESUME_TIMEOUT_S)
                if r.get("error"):
                    c.close()
                    ag_dead = True
                    pwlog("agent-view %s: session/messages unavailable "
                          "(%s) — file-driven CoT only"
                          % (sid[-8:], r["error"].get("message")))
                    return None
                ag_client.append(c)
            r = ag_client[0].request(
                "session/messages",
                {"sessionId": agent_session_of(aid), "limit": 60},
                timeout=AG_POLL_TIMEOUT_S)
            if r.get("error"):
                return None
            return (r.get("result") or {}).get("messages") or None
        except Exception:   # noqa: BLE001 — silent-by-design: deliberate degradation to the file path
            ag_dead = True
            return None

    def _agent_live_boxes() -> None:
        """The F57 flow over the agent's OWN messages: the filling box
        is born at its start (text-only), edited while it grows, frozen
        with its ag: button at the seal — the CoT streams LIVE from the
        pick (Damian, 26/10: hot CoT, live)."""
        msgs = _poll_agent_msgs()
        if msgs is None:
            return
        b2, l2, pending, kk = reasoning.live_boxes(msgs)
        starts, frozen = led.sync(b2, l2)
        for i0, r in starts:
            led.advance_to(r, i0)
        for i0, ph in frozen:
            idx_b = i0 + 1
            kb = {"inline_keyboard": _ag_rows(
                reasoning.agent_buttons(sid, t0, n, [led.labels[i0]],
                                        start=idx_b), n)}
            _edit(ph, reasoning.box_done_text(led.labels[i0],
                                              len(led.boxes[i0])), kb)
        _sync_view_lists()
        if boxes:
            ag_live[0] = True
            _save(live=not final_done)
        if not kk:
            return
        if led.live_ph and led.live_label == kk:
            _edit(led.live_ph, reasoning.box_live_text(title_a, kk, pending))
        else:
            ph_new = reasoning.ph_of(_say(
                reasoning.box_live_text(title_a, kk, pending)))
            if ph_new:
                led.live_birth(ph_new, kk)
                _save(live=not final_done)

    def _mine() -> bool:
        """F67 — the per-send step-down check: still our pid in the file
        (and a heartbeat touch, so a quiet view keeps its parent muted)."""
        try:
            with open(view_pid_path(sid)) as f:
                ok = int(f.read().strip() or 0) == os.getpid()
        except (OSError, ValueError):
            return False
        if ok:
            try:
                os.utime(view_pid_path(sid), None)
            except OSError:
                pass  # silent-by-design: beat touch, best-effort
        return ok

    def _save(**extra: Any) -> None:
        try:
            tmp = "%s.tmp.%d" % (agent_cache_path(sid, t0, n), os.getpid())
            with open(tmp, "w") as f:
                json.dump({"sid": sid, "t0": t0, "agent": n,
                           "title": "%s · agent %d" % (title, n),
                           "boxes": boxes, "labels": labels,
                           "phs": phs, "perbox": True, **extra}, f)
            os.replace(tmp, agent_cache_path(sid, t0, n))
        except OSError:
            pass  # silent-by-design: cache write, best-effort

    def _say(text: str, markup: Optional[Dict[str, Any]] = None) -> Any:
        """F67 — EVERY message of the view goes through here: no message
        once the pick left (26/10: tool boxes and narration were sent
        past the sweep because only the CoT boxes checked)."""
        if not _mine():
            raise _StepDown()
        return send(cfg, text, loud=False, markup=markup)

    def _edit(ph: Optional[int], text: str,
              markup: Optional[Dict[str, Any]] = None) -> None:
        if ph is None:
            return
        if not _mine():
            raise _StepDown()
        edit(cfg, ph, text, markup=markup)

    def _box(box: str, c: int) -> None:
        # F70 — collapsed: the box is stored for the taps, never sent on  # noqa: spanish — Damian's quoted request
        # its own; the pass lands as ONE bundle («un único botón con 17  # noqa: spanish — continuation
        # cots adentro»)  # noqa: spanish — continuation
        boxes.append(box)
        labels.append("A%d CoT %d.%d" % (n, pass_n, c))
        phs.append(None)

    def _bundle(first: int) -> None:
        """F70 — the pass's boxes land as ONE message with ONE button
        that unfolds the per-box keyboard on tap (agx:): the CoT stays
        one tap away without burying the chat (26/10: a 28-box request
        arrived as 28 messages — spam)."""
        nonlocal sent_n
        nb = len(boxes) - first
        if not nb:
            return
        kb = {"inline_keyboard": [[
            {"text": "📚 %d cots" % nb,
             "callback_data": "agx:%s:%d:%d:%d:%d"
             % (sid, t0, n, first + 1, nb)}]]}
        ph = reasoning.ph_of(_say(
            "📥 <b>A%d · pass %d landed</b> — %d cots · %s…%s"
            % (n, pass_n, nb, labels[first].split(" CoT ")[-1],
               labels[-1].split(" CoT ")[-1]), kb))
        for i in range(first, len(boxes)):
            phs[i] = ph        # every box of the pass expands IN PLACE
        reasoning.kb_log("agent bundle %d cots" % nb, ph, kb)
        sent_n += 1

    def _close_display() -> None:
        """F70b — the display at close: the filling box's live message
        ends as a cut line; boxes that never got their own message (bulk
        arrivals between polls — the F65b store) land as ONE collapsed
        bundle (the agx tap unfolds them) — never a message storm."""
        nonlocal sent_n
        if led.live_ph is not None:
            _edit(led.live_ph, reasoning.box_cut_text(title_a, led.live_label))
            led.live_ph = None
        missing = [i for i, p in enumerate(phs) if not p]
        if not missing:
            return
        kb = {"inline_keyboard": [[
            {"text": "📚 %d cots" % len(missing),
             "callback_data": "agx:%s:%d:%d:%d:%d"
             % (sid, t0, n, missing[0] + 1, len(missing))}]]}
        ph = reasoning.ph_of(_say(
            "📥 <b>%s · %d cots landed</b> — sealed while away · %s…%s"
            % (title_a, len(missing), labels[missing[0]].split(" CoT ")[-1],
               labels[missing[-1]].split(" CoT ")[-1]), kb))
        for i in missing:
            phs[i] = ph
        reasoning.kb_log("agent late bundle %d" % len(missing), ph, kb)
        sent_n += 1

    def _on_record(rec: Dict[str, Any]) -> None:
        nonlocal pass_n, final_done, sent_n
        first = len(boxes)
        resp = rec.get("response") or {}
        final_done = str(resp.get("finishReason") or "") == "stop"
        pass_n += 1
        reason = str(resp.get("reasoningText") or "").strip()
        # F70b — with the messages path alive the ledger already sealed
        # this pass's CoT live; the record would double-seal it. The
        # record keeps its OTHER cargo: tools, narration, the final.
        sealed = [] if ag_live[0] else (sealer.feed(reason) if reason else [])
        tcs = [t for t in resp.get("toolCalls") or [] if isinstance(t, dict)]
        text = str(resp.get("text") or "").strip()
        if final_done or tcs:
            sealed += sealer.close()   # the pass ended: seal the tail
        for _b, _c in sealed:
            _box(_b, _c)
        _bundle(first)
        if boxes:
            _save(live=not final_done)
        for tc in tcs:
            _say(_tool_box(str(tc.get("name") or "tool"),
                           tc.get("input") if isinstance(tc.get("input"), dict)
                           else {}))
            sent_n += 1
            if sent_n % 3 == 0:
                sleep(1.0)
        if text and not final_done:
            _say("🤖 <b>A%d · %s</b>\n%s"
                 % (n, esc(label), esc(text[:NARRATION_CAP])))
        if final_done:
            _final(text)

    def _final(text: str) -> None:
        for i in range(0, len(text), limits.CH_TURN):
            _say("✅ <b>A%d · %s — final answer</b>\n%s"
                 % (n, esc(label), esc(text[i:i + limits.CH_TURN])))
            sleep(0.4)

    # F68 — the pick is the starting line: the file's current end. Only
    # what the agent does from here on reaches the phone.
    try:
        pos = os.path.getsize(path)
    except OSError:
        pos = 0
    last = _last_record(path)
    done_at_pick = last is not None and str(
        (last.get("response") or {}).get("finishReason") or "") == "stop"
    try:
        if done_at_pick:
            _say("🎯 <b>Agent %d</b> · «%s» of «%s» — already finished; "
                 "its final answer:" % (n, esc(label), esc(title)))
            _final(str((last.get("response") or {}).get("text") or ""))
            return sent_n
        _say("🎯 <b>Watching Agent %d</b> · «%s» of «%s»\n(live from this "
             "moment — what it did before stays out; /task exits)"
             % (n, esc(label), esc(title)))
        last_live = now()
        states_at = -1e9
        # F69 — the in-flight beat: picked mid-request (a pure-reasoning
        # agent can think for half an hour with NOTHING on disk — verified
        # 26/10: session/messages is empty for subagent sessions and the
        # rollout only grows at request completion), the phone gets ONE
        # live line at once, edited by the minute, closed when it lands.
        # True mid-request CoT would need the fork to expose it.
        try:
            with open(path, "rb") as f:
                passes_before = f.read().count(b"\n")
        except OSError:
            passes_before = 0
        beat_ph: Optional[int] = None
        beat_born = last_live

        def _beat_text(k: int) -> str:
            # F70 — REAL activity, not a clock: the agent's own model.*
            # events (request in flight, iteration, network failures) and
            # the PIPE stamp (the runtime's last event age) — the line
            # proves the Mac↔Telegram pipe is connected and circulating
            act = activity() or {}
            if act.get("in_flight"):
                mins = max(1, int((now() - float(
                    act.get("started") or now())) / 60))
                bits = ["request in flight %d min" % mins]
                if act.get("iteration"):
                    bits.append("iter %d" % int(act["iteration"]))
                if act.get("net_fails"):
                    bits.append("net ✗ %d" % int(act["net_fails"]))
            else:
                bits = ["thinking… %d min"
                        % max(1, int((now() - last_live) / 60))]
            age = pipe_age()
            if age is not None:
                bits.append("pipe %ds" % max(0, int(age)))
            return ("🧠 <b>A%d · pass %d</b> — %s\n(the CoT streams live "
                    "as it thinks; late arrivals collapse into one bundle)"
                    % (n, k, " · ".join(bits)))

        while True:
            if not _mine():
                pwlog("agent-view %s: swept — stepping down" % sid[-8:])
                return sent_n   # /task swept the views — Damian's exit
            # F70b — the messages first: the live ledger seals the CoT as
            # it streams; the record that lands right after sees ag_live
            # and adds only its tools/narration/final (no double sealing,
            # no collapsed bundle for a pass that streamed live)
            if not final_done:
                _agent_live_boxes()
            records, pos = read_new(path, pos)
            for rec in records:
                _on_record(rec)
            if records:
                last_live = now()
                _edit(beat_ph, "✅ <b>A%d · pass %d landed</b> ↓"
                      % (n, passes_before + pass_n))
                beat_ph, beat_born = None, last_live
            if final_done:
                _close_display()
                if boxes:
                    _save(done=True)
                if _mine():
                    send(cfg, "↩️ <b>Agent %d closed</b> — back to the "
                              "task. (/subagents to see another)" % n,
                         loud=False)
                return sent_n
            # F68 — the runtime's word closes what the file never will
            # (stopped/failed/killed agents write no stop record): the
            # F65c log truth, by the row's aid
            if aid and now() - states_at >= STATES_EVERY_S:
                states_at = now()
                st = (states() or {}).get(aid)
                if st and st.get("state") != "running":
                    _close_display()
                    if boxes:
                        _save(done=True, error=str(st.get("state")))
                    _edit(beat_ph, "⏹ <b>A%d · pass %d</b> — %s"
                          % (n, passes_before + pass_n + 1,
                             esc(str(st.get("state")))))
                    if _mine():
                        send(cfg, "⏹ <b>Agent %d %s</b> — back to the task.%s"
                             % (n, esc(str(st.get("state"))),
                                ("\n" + esc(str(st.get("error"))))
                                if st.get("error") else ""), loud=False)
                    return sent_n
            if not aid and now() - last_live >= VIEW_IDLE_CUT_S \
                    and sid not in running():
                # legacy row (no aid): silence + parent gone = cut
                _close_display()
                if boxes:
                    _save(done=True, error="cut")
                _edit(beat_ph, "⏹ <b>A%d · pass %d</b> — cut"
                      % (n, passes_before + pass_n + 1))
                if _mine():
                    send(cfg, "⏹ <b>Agent %d cut</b> — its turn ended "
                              "before it answered." % n, loud=False)
                return sent_n
            if not final_done:
                k = passes_before + pass_n + 1
                mins = int((now() - last_live) / 60)
                if beat_ph is None and now() - beat_born >= VIEW_BEAT_BORN_S:
                    beat_ph = reasoning.ph_of(_say(_beat_text(k)))
                    beat_born = now()
                elif beat_ph is not None and now() - beat_born >= VIEW_BEAT_S:
                    _edit(beat_ph, _beat_text(k))
                    beat_born = now()
            wait([path], VIEW_WAIT_S)
    except _StepDown:
        # F67 — a /task pick (or a newer view) landed MID-BURST: not one
        # more message goes out
        pwlog("agent-view %s: swept mid-burst — stepping down" % sid[-8:])
        return sent_n
    finally:
        if ag_client:
            try:
                ag_client[0].close()
            except Exception:  # noqa: BLE001 — silent-by-design: closing a dead client
                pass
        try:
            os.remove(view_pid_path(sid))
        except OSError:
            pass  # silent-by-design: cleanup, best-effort


def cmd_agent_view(sid: str, n: str) -> None:
    """Entrypoint of the detached subprocess (gateway --agent-view)."""
    from zcode_bot.utils.config import load_config
    from zcode_bot.utils.logging import bind_context
    bind_context(sid=sid[-8:], component="agent-view")
    try:
        view_agent(load_config(), sid, int(n))
    except Exception as exc:  # noqa: BLE001 — the subprocess reports, not dies
        pwlog("agent-view %s failed: %s\n%s"
             % (sid[-8:], exc, traceback.format_exc()))