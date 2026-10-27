"""attach — plug into the session's RUNNING turn on /task (F62).

Damian, 24/10: «cuando selecciono una task... si está trabajando con  # noqa: spanish — Damian's quoted request
trabajo en curso, pueda ver inmediatamente los cot, los tools calls y  # noqa: spanish — continuation
todo, como conectarme inmediatamente al socket o lo que sea». There is  # noqa: spanish — continuation
no socket to plug into (ADR-017: the event bus belongs to the client
running the turn) — but the two file-based windows ARE attachable on
demand: the rollout (the spectator's tail) and the CLI history (the
CoT watcher). /task on a mid-turn session now:

- BOT worker turn: it is ALREADY streaming to this chat — the reply says
  so with the elapsed time and the CoT boxes sealed so far.
- Mac turn: CATCH-UP of what already happened (the rollout replayed from
  the marker — spectator._process's own parsing, COLLECTED instead of
  sent) + the two watchers ENSURED (single-flight: pid-files — nothing
  is spawned twice, dead watchers self-heal). An EXPLICIT attach
  overrides the mac_* knobs: they gate the automatic hook spawns, not a
  «show me now».

Imported LAZILY from handlers_tasks (the hooks→dispatch→handlers chain
must not reach spectator at import time).
"""
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from zcode_bot import config
from zcode_bot import live_io  # F73: the fork's live-CoT sidecar
from zcode_bot import reasoning
from zcode_bot import spectator
from zcode_bot import agent_live  # F63
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.logging import AttachError, log_exc, pwlog

CATCHUP_BOXES = 5   # recap boxes of the attach message (older steps: index)


def _shim_path() -> str:
    """hooks._shim_path, reached lazily (spectator imports hooks at module
    level — the reverse import here would close a cycle)."""
    from zcode_bot.hooks import _shim_path as p
    return p()


def _spawn_detached(args: List[str]) -> None:
    subprocess.Popen(
        [sys.executable, _shim_path()] + args,
        start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def turn_snapshot(sid: str, t0_ms: float,
                  read=None) -> Optional[Tuple[int, List[str]]]:
    """The turn SO FAR from the rollout: (steps, messages) where messages
    are the chat lines spectator._process WOULD have sent (boxes with
    their step number + intermediate narration). t0_ms pins the turn
    (the marker's mtime): records of previous turns do not exist here.
    None = no rollout at all (a very young turn — nothing happened yet
    or the file has not been created)."""
    if read is None:
        read = spectator._read_new
    sent: List[str] = []
    st: Dict[str, Any] = {"turn_id": None, "t0_ms": t0_ms, "step": 0,
                          "title": "",
          # collect EVERYTHING: the catch-up shows what the knobs would
          # have hidden — the attach is an explicit «show me now»
          "cfg": {"tools_stream": True, "tools_box_filter": "all"},
          "noticed": False, "log": None}

    def collect(text):
        sent.append(text)

    pos = 0
    while True:
        records, pos = read(config.rollout_path(sid), pos)
        if not records:
            break
        for record in records:
            try:
                spectator._process(record, st, collect)
            except Exception:  # noqa: BLE001 — the recap never breaks /task
                log_exc("errors", "recap record skipped (the /task recap continues)")
    return st["step"], sent


def catchup_text(title: str, steps: int, sent: List[str]) -> Optional[str]:
    """The recap message of an attach that RESPAWNED the spectator (a
    recap only makes sense when nothing was streaming: an alive
    spectator already put those boxes in the chat). The last
    CATCHUP_BOXES messages verbatim; older steps as a count. None =
    nothing to recap."""
    tail = [t for t in sent if not t.endswith("(spectator)")][-CATCHUP_BOXES:]
    older = steps - len(tail)
    if steps <= 0:
        return None
    head = "🖥️ <b>«%s»</b> — attaching mid-turn" % esc(title)
    if older > 0:
        head += " (+%d earlier steps)" % older
    return "\n".join([head] + tail)


def worker_summary(sid: str, ts: float, now=None) -> str:
    """The mid-turn reply when OUR worker owns the turn: it is already
    streaming everything to this chat — say so with the numbers the
    cache knows (the boxes sealed so far)."""
    mins = max(0, ((now or time.time)() - ts) / 60)
    n = 0
    try:
        with open(reasoning.cache_path(sid, int(ts))) as f:
            d = json.load(f)
        n = len(d.get("boxes") or [])
    except (OSError, ValueError):
        pass  # silent-by-design: probe/read, the except is the verdict
    return ("🔴 mid-turn — your run is ALREADY streaming to this chat "
            "(⏱ %d min · 🧠 %d CoT box%s so far — the tools too)"
            % (int(mins), n, "es" if n != 1 else ""))


def mac_attach(cfg: Dict[str, Any], sid: str, title: str, ws: str,
               deps: Optional[Dict[str, Any]] = None) -> str:
    """The mid-turn reply for a Mac turn: recap + watchers ensured. Sends
    the recap itself (it is a separate message, not part of /task's
    reply). deps: send/spawn/alive/watcher_alive (tests)."""
    d = deps or {}
    send = d.get("send", uhttp.send)
    spawn = d.get("spawn", _spawn_detached)
    spec_alive = d.get("alive", spectator.alive)
    watcher_alive = d.get("watcher_alive", reasoning.watcher_alive)
    agent_alive = d.get("agent_alive", agent_live.watcher_up)
    live_latest = d.get("live", live_io.latest)

    state = reasoning._marker_state(sid)
    if state is None:
        # no marker: the turn is closing (on_stop's race) or it is not an
        # app turn after all — the honest generic line
        return "⚠️ A turn is running right now (it is closing — or /cot reads it once it lands)."
    t0 = state[0]
    mins = int(max(0, time.time() - t0) / 60)
    steps, sent = turn_snapshot(sid, t0 * 1000.0)

    # the CoT watcher: single-flight exactly like on_user_prompt's spawn.
    # F72: "attach" — the respawn lands the boxes the chat never saw at
    # its FIRST sync (and inherits the predecessor's frozen messages)
    # F79b: a spawn failure raises AttachError (context: which watcher,
    # which sid) chained to the cause — the caller's wall logs it with
    # the full stack, and the F79c healer can catch it by type.
    def _spawn_watch(args, watcher):
        try:
            spawn(args)
        except OSError as e:
            raise AttachError("%s respawn failed" % watcher,
                              sid=sid[-8:], watcher=watcher) from e

    cot_was = "live" if watcher_alive(sid) else "dead"
    if cot_was == "dead":
        _spawn_watch(["--cot-live", sid, title, str(t0),
                      ws or os.getcwd(), "attach"], "cot-live")
    # the spectator: same — and only the RESPAWN needs the recap (an
    # alive one already delivered those boxes to the chat)
    spec_was = "live" if spec_alive(sid) else "dead"
    if spec_was == "dead":
        _spawn_watch(["--spectator", sid, ws or os.getcwd()], "spectator")
        recap = catchup_text(title, steps, sent)
        if recap:
            send(cfg, recap, loud=False)
    # F63: the subagent watcher rides along (its initial dir scan IS the
    # catch-up of agents that already ran)
    ag_was = "live" if agent_alive(sid) else "dead"
    if ag_was == "dead":
        _spawn_watch(["--agent-live", sid, title, str(t0),
                      ws or os.getcwd()], "agent-live")
    pwlog("attach %s: %dmin %dsteps cot=%s spec=%s"
          % (sid[-8:], mins, steps, cot_was, spec_was))
    # F73 — the fork's live-io sidecar: if the turn is THINKING right
    # now, say so with the real count (the runtime's own accumulator —
    # zero cost, pure file). None = nothing in flight: no line.
    live = live_latest(sid)
    live_line = ""
    if live:
        c = live_io.chars_line(live)
        live_line = "\n🧠 thinking right now · %s" % (c or "…")
    return ("🔴 mid-turn on the Mac (⏱ %d min · 🧰 %d steps so far)"
            "%s\n🖥 spectator: %s\n🧠 CoT watcher: %s\n🤖 agents: %s"
            % (mins, steps, live_line,
               "already streaming" if spec_was == "live"
               else "attached — respawned (it was dead)",
               "already live" if cot_was == "live"
               else "attached — respawned, boxes on the way",
               "watching" if ag_was == "live"
               else "watcher attached — boxes on the way"))