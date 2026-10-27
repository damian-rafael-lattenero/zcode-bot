"""healer — F79c: the channel's watchdog of CONTRACT.

The 27/10 14:25:48 incident, in one line: a turn ran on the Mac, the
CoT kept being produced, and NOBODY delivered it — the watcher inside a
live process was dead, every heartbeat F79a added was in place but
nobody READ them. This module is the reader that acts.

THE CONTRACT (pure, tested): a channel is dead-in-function when
  turn running
  + content fresh (the CoT cache or the fork's live-io sidecar moved
    within the silence window — something IS there to deliver)
  + delivery silent (prompt-worker.log's kb mtime older than the
    window, and/or the watcher's pid beat stale).
A quiet channel with NOTHING to deliver is healthy (nothing owed).

THE LADDER (poller-side, strikes per sid): strike 1 → re-attach through
the existing mac_attach path (single-flight respawn; the F78c bundle
lands whatever the chat never saw at the watcher's first sync); strike
2 → re-attach again + in-band report (💀 cause · 🔁); strike ≥3 → the
report says /nuclear (never an infinite respawn loop — one report per
episode, 10-min cooldown). Strikes reset on a healthy cycle.

The healer of the healer (the poller itself) is NOT this module: it is
the dumb launchd agent (scripts/bot-healer.py — mtimes only, no
network) that kickstarts the LaunchAgent when state/hb-transport goes
stale. Dumb on purpose: it must never be the thing that dies.
"""
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from zcode_bot import config
from zcode_bot import live_io
from zcode_bot import reasoning
from zcode_bot.core import active_bot_session
from zcode_bot.utils.logging import bind_context, get_logger, log_exc

SILENCE_S = 90.0        # legacy window (kb silence; kept for the report)
WATCHER_STALE_S = 45.0  # the watcher beats ~3s: stale >45s = definitively dead
SPAWN_GRACE_S = 90.0    # a just-born turn: the hook spawns within seconds —
                        # no pid-file yet is NOT a death until the turn is this old
HEAL_EVERY_S = 20.0     # the poller wakes ~3s; the heal itself throttles
EPISODE_COOLDOWN_S = 600.0   # one report episode per 10 min per sid
STRIKES_NUCLEAR = 3     # from here the report asks for /nuclear
_NO_STRIKES = 0         # a sid never seen has no strikes
_NEVER = 0.0            # episode epochs: never reported


@dataclass
class Obs:
    """Pure inputs — ages in SECONDS (now − mtime); None = no signal."""
    turn_running: bool = False
    turn_age: Optional[float] = None      # the marker's own age
    cache_age: Optional[float] = None     # newest cot-<sid>-*.json
    sidecar_age: Optional[float] = None   # live-io-<sid>.jsonl (the fork's)
    kb_age: Optional[float] = None        # prompt-worker.log (the kb channel)
    watcher_age: Optional[float] = None   # cot-live-<sid>.pid beat


def evaluate(obs: Obs, silence_s: float = SILENCE_S) -> str:
    """Pure contract → 'ok' | 'no_turn' | 'dead_channel'.

    F79c2 — the owed thing is the LINE ITSELF: while a turn runs, the
    chat is owed a live, blinking thinking line (F80's lesson, the
    18:00-18:02 dead window of 27/10: a long tool produced no fresh
    content, the F79c contract said «nothing owed» and the chat went
    emoticon-less exactly while the Mac worked). Content freshness NO
    LONGER gates the heal — a stale WATCHER alone is the death. The
    turn-age grace keeps the spawn race out: at turn start the hook
    spawns the watcher within seconds; no pid-file on a YOUNG turn is
    the hook on its way, not a death."""
    if not obs.turn_running:
        return "no_turn"
    if obs.watcher_age is None:
        if obs.turn_age is not None and obs.turn_age > SPAWN_GRACE_S:
            return "dead_channel"   # old turn, watcher never came
        return "ok"                 # the hook is on its way
    if obs.watcher_age > WATCHER_STALE_S:
        return "dead_channel"
    return "ok"


# --------------------------------------------------------- observation

def _age(path: str, now: float) -> Optional[float]:
    try:
        return max(0.0, now - os.path.getmtime(path))
    except OSError:
        return None


def _cache_age(sid: str, now: float) -> Optional[float]:
    """Newest cot-<sid>-*.json in state/ (any turn of the session)."""
    best: Optional[float] = None
    try:
        names = os.listdir(config.STATE_DIR)
    except OSError:
        return None
    for n in names:
        if n.startswith("cot-%s-" % sid) and n.endswith(".json"):
            a = _age(os.path.join(config.STATE_DIR, n), now)
            if a is not None and (best is None or a < best):
                best = a
    return best


def observe(sid: str, now: Optional[float] = None) -> Obs:
    """Gather the contract's inputs for one session (lstat-only)."""
    now = now if now is not None else time.time()
    marker = reasoning._marker_state(sid)   # the Mac's own turn marker
    running = marker is not None
    return Obs(
        turn_running=running,
        turn_age=_age(config.marker_path(sid), now) if running else None,
        cache_age=_cache_age(sid, now),
        sidecar_age=_age(live_io.sidecar_path(sid), now),
        kb_age=_age(os.path.join(config.BASE, "hooks", "logs",
                                 "prompt-worker.log"), now),
        watcher_age=_age(os.path.join(
            config.STATE_DIR, "cot-live-%s.pid" % sid), now))


def _last_cause() -> str:
    """The last recorded cause in the F79b event log — the report's
    'why' (first frame of the stack; 'unknown' when the death left
    nothing, which after F79a should not happen again)."""
    try:
        with open(os.path.join(config.LOG_DIR, "errors.log")) as f:
            for ln in reversed(f.readlines()[-400:]):
                if "cause=" in ln:
                    return ln.split("cause=", 1)[1].strip()[:120]
    except OSError:
        pass  # silent-by-design: the report degrades to 'unknown'
    return "no cause recorded"


# ------------------------------------------------------------- acting

_strikes: Dict[str, int] = {}
_last_episode: Dict[tuple, float] = {}   # (sid, rung) → last report epoch
_last_check = 0.0


def check(cfg: Dict[str, Any], deps: Optional[Dict[str, Any]] = None,
          now: Optional[float] = None) -> Optional[str]:
    """The poller-side action (throttled internally). Returns the report
    text when one was sent, else None. deps: observe/evaluate/attach/
    send for tests."""
    global _last_check
    now = now if now is not None else time.time()
    if now - _last_check < HEAL_EVERY_S:
        return None
    _last_check = now
    d = deps or {}
    _observe = d.get("observe", observe)
    _attach = d.get("attach", _mac_attach)
    _send = d.get("send", _send_report)
    act = active_bot_session()   # BotSession: compat .get (core's mapping)
    sid = act.get("focus_sid") or act.get("sid") or ""
    if not sid or not act.get("ws"):
        return None
    verdict = evaluate(_observe(sid, now))
    if verdict != "dead_channel":
        _strikes.pop(sid, None)
        return None
    n = _strikes.get(sid, _NO_STRIKES) + 1
    _strikes[sid] = n
    cause = _last_cause()
    if n >= STRIKES_NUCLEAR:
        # the advice fires once per episode (per-cooldown, its own rung:
        # the strike-2 report must not drown the ☢️ escalation)
        if now - _last_episode.get((sid, "nuclear"), _NEVER) < EPISODE_COOLDOWN_S:
            return None
        _last_episode[(sid, "nuclear")] = now
        return _send(cfg,
                     "💀 The channel of %s is STILL dead after %d heals "
                     "(cause: %s).\n☢️ /nuclear — the full restart is the "
                     "honest fix now." % (sid[-8:], n - 1, cause), now)
    t0 = time.time()
    try:
        _attach(cfg, sid, (act.get("title") or "") or sid[-8:],
                act.get("ws") or "")
        revived_s = time.time() - t0
    except Exception:
        log_exc("errors", "healer: the re-attach itself failed")
        revived_s = -1.0
    if n == 1:
        return None   # the first heal is silent: F78c's 📥 bundle reports the landing
    if now - _last_episode.get((sid, "report"), _NEVER) < EPISODE_COOLDOWN_S:
        return None
    _last_episode[(sid, "report")] = now
    return _send(cfg,
                 "💀 %s channel died (%s) · 🔁 revived in %.1fs (heal #%d) "
                 "— the 📥 bundle lands what the chat never saw."
                 % (sid[-8:], cause, max(0.0, revived_s), n), now)


def _mac_attach(cfg: Dict[str, Any], sid: str, title: str, ws: str) -> str:
    from zcode_bot.attach import mac_attach   # late: no import cycle at load
    bind_context(sid=sid[-8:], component="healer")
    return mac_attach(cfg, sid, title, ws)


def _send_report(cfg: Dict[str, Any], text: str, now: float) -> str:
    from zcode_bot.utils import http as uhttp
    uhttp.send(cfg, text, loud=True)
    get_logger("healer").info("episode reported")
    return text


def health_line(now: Optional[float] = None) -> str:
    """F79d — the one-line channel health for /status and /handshake:
    every heartbeat age + the contract's verdict over the focus."""
    now = now if now is not None else time.time()
    parts = []
    try:
        from zcode_bot.utils.http import breaker_state  # F79e input
        parts.append("breaker %s" % breaker_state())
    except Exception:  # noqa: BLE001 — silent-by-design: the line degrades, never breaks
        pass
    t = _age(os.path.join(config.STATE_DIR, "hb-transport"), now)
    parts.append("transport %s" % ("—" if t is None else
                                   ("%.0fs" % t if t < 300 else "STALE %.0fs" % t)))
    act = active_bot_session()
    sid = act.get("focus_sid") or act.get("sid") or ""
    verdict = "no focus"
    if sid:
        w = _age(os.path.join(config.STATE_DIR, "cot-live-%s.pid" % sid), now)
        parts.append("cot-live %s" % ("—" if w is None else
                                      ("%.0fs" % w if w < 300 else "STALE")))
        verdict = evaluate(observe(sid, now))
    return "Channel: %s · contract: %s" % (" · ".join(parts), verdict)


def reset_bookkeeping() -> None:
    """Tests: strikes/cooldowns/throttle back to zero."""
    global _last_check
    _strikes.clear()
    _last_episode.clear()
    _last_check = 0.0
