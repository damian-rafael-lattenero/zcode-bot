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
from typing import Any, Dict, List, Optional, Tuple

from zcode_bot import config
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
        pass


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

    def __init__(self, n: int, label: str, prompt: str) -> None:
        self.n = n
        self.label = label
        self.prompt = prompt
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
        # show any session's agents, the Mac's included
        self.may_push = d.get("may_push",
                              lambda: _task_current_ok(self.cfg, self.sid))

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
                           "updated": self.now(),
                           "agents": [{"n": a.n, "label": a.label,
                                       "prompt": a.prompt[:160],
                                       "state": a.state, "req_n": a.req_n,
                                       "path": a.path or "",
                                       "last": a.last_line}
                                      for a in self.agents]}, f)
            os.replace(tmp, index_path(self.sid))
        except OSError:
            pass

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
            a = _Agent(len(self.agents) + 1,
                       str(r.get("label") or "?"), str(r.get("prompt") or ""))
            self.agents.append(a)
            self._birth(a)
            self._index_write()

    # -------------------------------------------------- file assignment
    def assign_files(self) -> None:
        """Agent rollout files born this turn → assign FIFO to the
        registered agents (a file without a spawn entry registers an
        implicit agent — attach mid-turn without the label)."""
        pending = [a for a in self.agents if a.path is None and not a.done]
        for path in _agent_files():
            if path in self._files_seen:
                continue
            meta = _first_record(path)
            if meta is None or meta["ms"] is None:
                continue              # born, no completed request yet
            if meta["ms"] < self.t0_ms - BORN_SLACK_MS:
                self._files_seen.add(path)   # previous turn's agent
                continue
            if self.ws and self.ws.rstrip("/") not in meta["raw"]:
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
                # no spawn entry (attach mid-turn, spectator missing): the
                # agent's own prompt is its label
                a = _Agent(len(self.agents) + 1,
                           meta["prompt"][:60] or "subagent",
                           meta["prompt"][:160])
                self.agents.append(a)
                self._birth(a)
            a.path = path
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
            a.last_line = a.last_line or "cut before its first request"
            self._push_edit(a.live_ph, self._status_text(a, reason))
        self._index_write()

    def heartbeat(self) -> None:
        """The long-request heartbeat (the file only grows per completed
        request — a single request can take minutes)."""
        now = self.now()
        for a in self.agents:
            if a.done or a.live_ph is None:
                continue
            if now - a.last_beat >= HEARTBEAT_S:
                a.last_beat = now
                self._push_edit(a.live_ph, self._status_text(a, "running")
                                + "\n⏱ still on it (%d min · req %d in flight)"
                                % (int((now - self.t0) / 60), a.req_n + 1))

    def _save(self, a: _Agent, **extra: Any) -> None:
        try:
            tmp = "%s.tmp.%d" % (agent_cache_path(self.sid, self.t0, a.n),
                                 os.getpid())
            with open(tmp, "w") as f:
                json.dump({"sid": self.sid, "t0": self.t0, "agent": a.n,
                           "title": "%s · agent %d" % (self.title, a.n),
                           **a.cache_kwargs(), **extra}, f)
            os.replace(tmp, agent_cache_path(self.sid, self.t0, a.n))
        except OSError:
            pass


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
    """Single-flight: a live watcher spawned for THIS turn. A pid-file
    older than the current marker is a previous turn's watcher (25/10:
    a stale one blocked the new spawn and the turn went mute)."""
    try:
        with open(watcher_pid_path(sid)) as f:
            pid = int(f.read().strip() or 0)
        pid_mt = os.path.getmtime(watcher_pid_path(sid))
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if pid_mt < os.path.getmtime(config.marker_path(sid)) - 1.0:
            return False
    except OSError:
        pass  # no marker (phone turn): the pid alone decides
    if proc_alive is None:
        from zcode_bot.workers import _proc_alive  # lazy
        proc_alive = _proc_alive
    return bool(proc_alive(pid))


def watch_agents(cfg: Dict[str, Any], sid: str, title: str, t0: float, ws: str,
          deps: Optional[Dict[str, Any]] = None) -> int:
    """The watcher loop: one turn, all its agents. Ends when the session
    leaves running_turns() (app and headless turns both register there)
    or at the cap. Returns the boxes sent."""
    from zcode_bot import fswatch
    d = deps or {}
    now = d.get("now", time.time)
    running = d.get("running", running_turns)
    wait = d.get("wait", fswatch.wait_change)
    ta = TurnAgents(cfg, sid, title, t0, deps=dict(d, ws=ws))
    ta.summary = vb.level_of(cfg) == "summary"
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(watcher_pid_path(sid), "w") as f:
        f.write(str(os.getpid()))
    deadline = now() + WATCH_CAP_S
    paths = [_dir(), spawn_log_path(sid)]
    boxes = 0
    started = now()
    try:
        while now() < deadline:
            if config.code_changed():
                pwlog("agent-live %s: new code deployed — stepping down"
                      % sid[-8:])
                return boxes
            # the turn is alive while the CLI log says so OR the Mac's
            # marker stands; and never during the startup grace — the
            # hook fires BEFORE turn.started lands in the log (25/10:
            # watchers dying in 1s with agents=0)
            if now() - started > START_GRACE_S \
                    and sid not in running() \
                    and not os.path.exists(config.marker_path(sid)):
                break
            ta.poll_spawns()
            ta.assign_files()
            ta.poll_agents()
            ta.heartbeat()
            boxes = sum(len(a.boxes) for a in ta.agents)
            wait(paths, timeout=WATCH_S)
        ta.cut_all("turn ended")
        boxes = sum(len(a.boxes) for a in ta.agents)
        pwlog("agent-live end: agents=%d boxes=%d"
              % (len(ta.agents), boxes))
        return boxes
    finally:
        try:
            os.remove(watcher_pid_path(sid))
        except OSError:
            pass


def cmd_agent_live(sid: str, title: str, t0: str, ws: str) -> None:
    """Entrypoint of the detached subprocess (gateway --agent-live)."""
    from zcode_bot.utils.config import load_config
    try:
        watch_agents(load_config(), sid, title, float(t0), ws)
    except Exception as exc:  # noqa: BLE001 — the subprocess reports, not dies
        pwlog("agent-live %s failed: %s" % (sid[-8:], exc))


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
    t = (now or time.time)()
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
        kb.append([{"text": ("%s %d · %s" % (mark, a["n"],
                                              a.get("label") or "?"))[:36],
                    "callback_data": "sa:%s:%d:%d" % (sid, t0, a["n"])}])
    return lines, kb


def subagents_list(sid: str, title: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """/subagents: the agents of the active task's CURRENT turn — one
    button each with its descriptive title (Damian: «que tenga su título  # noqa: spanish — Damian's quoted request
    descriptivo»), THEN the other sessions with agents (F64b: the Mac's  # noqa: spanish — continuation
    turns — Damian: «¿podría ver los subagents de la Mac?»). Pick one →  # noqa: spanish — Damian's quoted request
    its run, shown like a task's; the button carries its session."""
    idx = read_index(sid) if sid else {}
    lines: List[str] = []
    kb: List[List[Dict[str, str]]] = []
    if idx.get("agents"):
        lines.append("🤖 <b>Subagents of «%s»</b> 🎯" % esc(title))
        l2, k2 = _agent_rows(idx)
        lines += l2
        kb += k2
    else:
        lines.append("🤖 No subagents in «%s»'s current turn." % esc(title))
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
            pass
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
    idx = read_index(sid)
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
                pass


def _resolve_agent_file(sid: str, n: int) -> Optional[Dict[str, Any]]:
    """The n-th agent's rollout file: the index carries the path (the
    watcher writes it on assignment); older indexes fall back to a scan
    by prompt (the linkage F63c verified)."""
    idx = read_index(sid)
    meta: Dict[str, Any] = next(
        (a for a in (idx.get("agents") or []) if a.get("n") == n), {})
    if meta.get("path") and os.path.exists(meta["path"]):
        return {"path": meta["path"], "label": meta.get("label") or "agent %d" % n,
                "title": idx.get("title") or sid[-8:]}
    prompt = str(meta.get("prompt") or "")
    if prompt:
        for path in _agent_files():
            m = _first_record(path)
            if m and m["prompt"].startswith(prompt[:80]):
                return {"path": path, "label": meta.get("label") or "agent %d" % n,
                        "title": idx.get("title") or sid[-8:]}
    return None


def view_agent(cfg: Dict[str, Any], sid: str, n: int,
               deps: Optional[Dict[str, Any]] = None) -> int:
    """F65 — picking a subagent IS picking a task (Damian: «el subagente  # noqa: spanish — Damian's quoted request
    hijo es exactamente igual al padre que lo creó, en cuerpo y alma»).  # noqa: spanish — continuation
    One detached view per parent session: CATCH-UP of everything the
    agent did + live tail, in the task's own format — 🧠 its reasoning
    in size-sealed CoT boxes with the ag: buttons (born open in
    verbose, F59 in-place on tap), 🔵 a box per tool it runs, 🖥️ its
    narration, and its FULL final answer at close, then ↩️ back. Only
    the chosen one streams; /task sweeps it away (stop_views)."""
    from zcode_bot import fswatch
    d = deps or {}
    send = d.get("send", uhttp.send)
    wait = d.get("wait", lambda p, timeout: fswatch.wait_change(p, timeout))
    read_new = d.get("read_new", _read_new)
    now = d.get("now", time.time)
    sleep = d.get("sleep", time.sleep)
    running = d.get("running", running_turns)
    res = _resolve_agent_file(sid, n)
    if res is None:
        send(cfg, "🤔 That agent's file is gone (older turn — its boxes "
                  "expired with the rotation).")
        return 0
    path, label, title = res["path"], res["label"], res["title"]
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
            pass

    def _box(box: str, c: int) -> None:
        nonlocal sent_n
        lab = "A%d CoT %d.%d" % (n, pass_n, c)
        boxes.append(box)
        labels.append(lab)
        idx_b = len(boxes)
        if vb.born_open(cfg):
            text, kb = reasoning.box_open_view(
                sid, t0, idx_b, lab, "%s · agent %d" % (title, n), box)
            kb["inline_keyboard"] = _ag_rows(kb["inline_keyboard"], n)
        else:
            text = "🧠 <b>%s</b> · %s" % (esc(lab), reasoning.fmt_tok(len(box)))
            kb = {"inline_keyboard": reasoning.agent_buttons(sid, t0, n, [lab],
                                                             start=idx_b)}
        ph = reasoning.ph_of(send(cfg, text, loud=False, markup=kb))
        phs.append(ph)
        reasoning.kb_log("agent view box %s" % lab, ph, kb)
        sent_n += 1
        if sent_n % 3 == 0:
            sleep(1.0)

    def _on_record(rec: Dict[str, Any]) -> None:
        nonlocal pass_n, final_done, sent_n
        resp = rec.get("response") or {}
        final_done = str(resp.get("finishReason") or "") == "stop"
        pass_n += 1
        reason = str(resp.get("reasoningText") or "").strip()
        sealed = sealer.feed(reason) if reason else []
        tcs = [t for t in resp.get("toolCalls") or [] if isinstance(t, dict)]
        text = str(resp.get("text") or "").strip()
        if final_done or tcs:
            sealed += sealer.close()   # the pass ended: seal the tail
        for _b, _c in sealed:
            _box(_b, _c)
        if boxes:
            _save(live=not final_done)
        for tc in tcs:
            send(cfg, _tool_box(str(tc.get("name") or "tool"),
                                tc.get("input") if isinstance(tc.get("input"), dict)
                                else {}), loud=False)
            sent_n += 1
            if sent_n % 3 == 0:
                sleep(1.0)
        if text and not final_done:
            send(cfg, "🤖 <b>A%d · %s</b>\n%s"
                 % (n, esc(label), esc(text[:NARRATION_CAP])), loud=False)
        if final_done:
            for i in range(0, len(text), limits.CH_TURN):
                send(cfg, ("✅ <b>A%d · %s — final answer</b>\n%s"
                           % (n, esc(label), esc(text[i:i + limits.CH_TURN]))),
                     loud=False)
                sleep(0.4)

    send(cfg, "🎯 <b>Watching Agent %d</b> · «%s» of «%s»\\n(everything it did "
              "arrives below, live until it closes — /task exits)"
         % (n, esc(label), esc(title)), loud=False)
    pos = 0
    try:
        while True:
            try:
                with open(view_pid_path(sid)) as f:
                    if int(f.read().strip() or 0) != os.getpid():
                        pwlog("agent-view %s: another pick took over" % sid[-8:])
                        return sent_n
            except (OSError, ValueError):
                return sent_n   # /task swept the views — Damian's exit
            records, pos = read_new(path, pos)
            for rec in records:
                _on_record(rec)
            if final_done:
                if boxes:
                    _save(done=True)
                send(cfg, "↩️ <b>Agent %d closed</b> — back to the task. "
                          "(/subagents to see another)", loud=False)
                return sent_n
            if not records and not _first_heartbeat_ok(path, pos, now):
                # the parent's turn is over and nothing new is coming:
                # the agent died with it (no stop record) — close as cut
                if sid not in running():
                    if boxes:
                        _save(done=True, error="cut")
                    send(cfg, "⏹ <b>Agent %d cut</b> — its turn ended before "
                              "it answered." % n, loud=False)
                    return sent_n
            wait([path], VIEW_WAIT_S)
    finally:
        try:
            os.remove(view_pid_path(sid))
        except OSError:
            pass


def _first_heartbeat_ok(path: str, pos: int, now) -> bool:
    """The young-agent guard: a file with no completed record yet (pos 0,
    empty) is a live agent whose FIRST request is still in flight — give
    it the turn's window, don't cut it."""
    if pos > 0:
        return True
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def cmd_agent_view(sid: str, n: str) -> None:
    """Entrypoint of the detached subprocess (gateway --agent-view)."""
    from zcode_bot.utils.config import load_config
    try:
        view_agent(load_config(), sid, int(n))
    except Exception as exc:  # noqa: BLE001 — the subprocess reports, not dies
        pwlog("agent-view %s failed: %s" % (sid[-8:], exc))
