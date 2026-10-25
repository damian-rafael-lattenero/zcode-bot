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
from typing import Any, Dict, List, Optional

from zcode_bot import config
from zcode_bot import reasoning
from zcode_bot import verbosity as vb
from zcode_bot.core import running_turns
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.logging import pwlog

WATCH_CAP_S = 35 * 60   # (own name: the anti-dup gate)
WATCH_S = 2.0          # loop cadence when fswatch is instant/absent
HEARTBEAT_S = 90       # "still on it" edit cadence of an agent's live line
BORN_SLACK_MS = 15000  # agent files: first record within this of t0 counts
REASON_KEEP = 2400     # monster reasoningText: head+tail rendered, not all
REASON_HEAD = 200
TEXT_KEEP = 800

# the tools a PARENT uses to spawn an agent (the label handoff listens
# for these names on both surfaces)
AGENT_TOOLS = ("Task", "Agent")

AGENT_FILE_PREFIX = "model-io-sess_subagent_agent-"


def spawn_log_path(sid: str) -> str:
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in sid)
    return os.path.join(config.STATE_DIR, "agent-spawn-%s.jsonl" % safe)


def agent_cache_path(sid: str, t0: float, n: int) -> str:
    """The n-th agent of the turn (sid, t0): SAME shape as a CoT cache
    (boxes/labels/phs/perbox) so the taps, parts and the F59 in-place
    expansion all reuse the CoT machinery."""
    return os.path.join(config.STATE_DIR,
                        "agent-%s-%d-%d.json" % (sid, int(t0), n))


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


def _first_started_ms(path: str) -> Optional[float]:
    """startedAt (epoch ms) of the file's FIRST record — None if the
    file is empty or unreadable (born but not one request done yet)."""
    try:
        with open(path, errors="replace") as f:
            ln = f.readline()
        if not ln.strip():
            return None
        return _ts_ms(json.loads(ln).get("startedAt"))
    except (OSError, ValueError):
        return None


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
        self.summary = False   # set by watch() from the verbosity level

    # -------------------------------------------------- label handoff
    def poll_spawns(self) -> None:
        """New spawn-file entries of THIS turn → register + birth the
        live message (the spawn arrives instantly; the file lags until
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
            if not self.summary:
                ph = reasoning.ph_of(self.send(
                    self.cfg, "🤖 <b>Agent %d</b> · %s — working…\n%s"
                    % (a.n, esc(a.label), esc(a.prompt[:160])), loud=False))
                a.live_ph = ph

    # -------------------------------------------------- file assignment
    def assign_files(self) -> None:
        """Agent rollout files born this turn → assign FIFO to the
        registered agents (a file without a spawn entry registers an
        implicit agent — attach mid-turn without the label)."""
        pending = [a for a in self.agents if a.path is None and not a.done]
        for path in _agent_files():
            if path in self._files_seen:
                continue
            ms = _first_started_ms(path)
            if ms is None:            # born, no completed request yet
                continue
            if ms < self.t0_ms - BORN_SLACK_MS:
                self._files_seen.add(path)   # previous turn's agent
                continue
            self._files_seen.add(path)
            if pending:
                a = pending.pop(0)
            else:
                a = _Agent(len(self.agents) + 1, "subagent", "")
                self.agents.append(a)
                if not self.summary:
                    a.live_ph = reasoning.ph_of(self.send(
                        self.cfg, "🤖 <b>Agent %d</b> — working…" % a.n,
                        loud=False))
            a.path = path

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
        chunk = render_record(rec)
        sealed = a.sealer.feed(chunk) if chunk else []
        if final:
            sealed += a.sealer.close()
        for _box, _c in sealed:
            self._freeze(a, _box, _c)
        if a.boxes:
            self._save(a, live=not final)
        if final:
            a.done = True
            self._close(a, str(resp.get("text") or ""))
        elif a.live_ph and not self.summary:
            tcs = [t for t in resp.get("toolCalls") or []
                   if isinstance(t, dict)]
            tail = (_tool_line(str(tcs[-1].get("name") or "tool"),
                               tcs[-1].get("input") or {})
                    if tcs else "thinking…")
            self.edit(self.cfg, a.live_ph,
                      "🤖 <b>Agent %d</b> · %s — req %d done, working… (%s)"
                      % (a.n, esc(a.label), a.req_n, esc(tail[:70])))

    def _freeze(self, a: _Agent, box: str, c: int) -> None:
        """The seal (F57 model): the box's OWN message, born frozen with
        ITS button «Agent N.C». Verbose: born OPEN (box_open_view) with
        the ⤴️ of the F59 collapse."""
        label = "Agent %d.%d" % (a.n, c)
        a.boxes.append(box)
        a.labels.append(label)
        n_box = len(a.boxes)
        if vb.born_open(self.cfg):
            text, kb = reasoning.box_open_view(
                self.sid, self.t0, n_box, label, "%s · agent %d" % (
                    self.title, a.n), box)
            kb["inline_keyboard"] = _ag_rows(kb["inline_keyboard"], a.n)
        else:
            text = "🤖 <b>%s</b> · %s" % (esc(label),
                                          reasoning.fmt_tok(len(box)))
            kb = {"inline_keyboard": reasoning.agent_buttons(
                self.sid, self.t0, a.n, [label], start=c)}
        ph = reasoning.ph_of(self.send(self.cfg, text, loud=False, markup=kb))
        a.phs.append(ph)
        reasoning.kb_log("agent box %s" % label, ph, kb)
        self._resend_live(a)

    def _resend_live(self, a: _Agent) -> None:
        """The live line stays at the BOTTOM (boxes froze below it):
        delete + re-send, F7's live_moved lesson."""
        if a.live_ph is None or self.summary:
            return
        self.delete(self.cfg, a.live_ph)
        a.live_ph = reasoning.ph_of(self.send(
            self.cfg, "🤖 <b>Agent %d</b> · %s — working… (req %d · %d boxes)"
            % (a.n, esc(a.label), a.req_n, len(a.boxes)), loud=False))

    def _close(self, a: _Agent, final_text: str) -> None:
        if a.live_ph is not None:
            self.edit(self.cfg, a.live_ph,
                      "✅ <b>Agent %d</b> · done — %s"
                      % (a.n, esc(final_text[:180])))
        elif self.summary:
            # summary: no boxes, no live line — the RESULT still arrives
            self.send(self.cfg, "✅ <b>Agent %d</b> · done — %s"
                      % (a.n, esc(final_text[:280])), loud=False)
        self._save(a, done=True)

    def cut_all(self, reason: str) -> None:
        """Turn end: seal the tails and mark the cuts (an agent killed
        mid-flight never writes its stop record)."""
        for a in self.agents:
            if a.done:
                continue
            if a.path is not None:
                for _box, _c in a.sealer.close():
                    self._freeze(a, _box, _c)
                if a.boxes:
                    self._save(a, live=False)
            if a.live_ph is not None:
                self.edit(self.cfg, a.live_ph,
                          "⏹ <b>Agent %d</b> · cut (%s)" % (a.n, esc(reason)))

    def heartbeat(self) -> None:
        """The long-request heartbeat (the file only grows per completed
        request — a single request can take minutes)."""
        now = self.now()
        for a in self.agents:
            if a.done or a.live_ph is None or self.summary:
                continue
            if now - a.last_beat >= HEARTBEAT_S:
                a.last_beat = now
                self.edit(self.cfg, a.live_ph,
                          "🤖 <b>Agent %d</b> · %s — still on it (%d min · "
                          "req %d in flight)"
                          % (a.n, esc(a.label), int((now - self.t0) / 60),
                             a.req_n + 1))

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
    try:
        with open(watcher_pid_path(sid)) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
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
    ta = TurnAgents(cfg, sid, title, t0, deps=d)
    ta.summary = vb.level_of(cfg) == "summary"
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(watcher_pid_path(sid), "w") as f:
        f.write(str(os.getpid()))
    deadline = now() + WATCH_CAP_S
    paths = [_dir(), spawn_log_path(sid)]
    boxes = 0
    try:
        while now() < deadline:
            if config.code_changed():
                pwlog("agent-live %s: new code deployed — stepping down"
                      % sid[-8:])
                return boxes
            if sid not in running():
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
