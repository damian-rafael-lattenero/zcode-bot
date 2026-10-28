"""zconn — F85b: the z.ai connection's voice in the chat.

The fork's pills already show it on the Mac (deterministic retry curve
2^n to 1024s, f85a67f6) — but the chat was deaf to it: Damian cut the
internet briefly (28/10) and the bot said NOTHING. The truth the pills
read is the CLI log, and the bot already tails it (cli_logs, F78a):

- DOWN  = model.network.failed / model.request.failed (warn pairs;
  context.statusMessage carries the human reason — «Cannot connect to
  API: read ETIMEDOUT», ENOTFOUND, 502, «other side closed»)
- UP    = model.network.completed

This module feeds those into ONE episode state machine and speaks on
TRANSITIONS only: one ⚠️ when the cut starts (with the reason), one ✅
when it heals (with how long it lasted). If the Mac's own internet is
the corpse, the ⚠️ parks in the outbox and BOTH notices land together
the moment the wire returns — the outage still gets its story told.

Sputters (a fail seconds after a recovery — the same sick connection)
don't re-announce: one episode, one pair, one duration spanning it all.
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

from zcode_bot import config
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.logging import log

DOWN_EVENTS = ("model.network.failed", "model.request.failed")
UP_EVENTS = ("model.network.completed",)
NEEDLES = DOWN_EVENTS + UP_EVENTS
SPUTTER_S = 120.0        # a re-fail this close to a recovery = same episode
CHECK_STATE = "zconn.json"


def _fmt_dur(s: float) -> str:
    s = int(s)
    if s < 60:
        return "%ds" % s
    return "%dm %02ds" % (s // 60, s % 60)


class ConnTrack:
    """Pure episode machine: feed(ts, event, reason) → notices.
    ("down", reason, ts) once per episode; ("up", first_down, ts) when
    it heals. Sputters of the same episode stay quiet and the UP keeps
    the episode's FIRST down as its anchor (the duration is the whole
    story, not the last splutter)."""

    def __init__(self) -> None:
        self.down = False
        self.first_down: Optional[float] = None
        self.last_up: Optional[float] = None
        self.reason = ""

    def feed(self, ts: float, ev: str,
             reason: str = "") -> List[tuple]:
        if ev in DOWN_EVENTS:
            if not self.down:
                self.down = True
                fresh = (self.first_down is None or self.last_up is None
                         or ts - self.last_up >= SPUTTER_S)
                if fresh:
                    self.first_down = ts
                    self.reason = reason or "connection failed"
                    return [("down", self.reason, ts)]
                return []   # sputter: the chat already knows this episode
            if reason:
                self.reason = reason   # escalate silently (502 → ENOTFOUND)
            return []
        if ev in UP_EVENTS:
            self.last_up = ts
            if self.down:
                self.down = False
                assert self.first_down is not None
                return [("up", self.first_down, ts)]
        return []

    def as_dict(self) -> Dict[str, Any]:
        return {"down": self.down, "first_down": self.first_down,
                "last_up": self.last_up, "reason": self.reason}


def _load_track():
    try:
        with open(os.path.join(config.STATE_DIR, CHECK_STATE)) as f:
            d = json.load(f)
        t = ConnTrack()
        t.down = bool(d.get("down"))
        t.first_down = d.get("first_down")
        t.last_up = d.get("last_up")
        t.reason = str(d.get("reason") or "")
        return t, float(d.get("since") or 0.0)
    except (OSError, ValueError):
        return None, 0.0


def _save_track(t: ConnTrack, since: float) -> None:
    try:
        d = t.as_dict()
        d["since"] = since
        with open(os.path.join(config.STATE_DIR, CHECK_STATE), "w") as f:
            json.dump(d, f)
    except OSError:
        pass  # silent-by-design: worst case a notice repeats once


def _notify(cfg: Dict[str, Any], notice: tuple) -> None:
    if notice[0] == "down":
        uhttp.send(cfg, "⚠️ <b>z.ai connection lost</b>\n%s"
                   % str(notice[1]), loud=True)
    else:   # ("up", first_down, ts)
        uhttp.send(cfg, "✅ <b>z.ai back</b> — the cut lasted %s"
                   % _fmt_dur(float(notice[2]) - float(notice[1])),
                   loud=True)


def listen(cfg: Dict[str, Any], iter_ev=None, now: Optional[float] = None
          ) -> List[tuple]:
    """The poller's cycle hook: tail the CLI log for DOWN/UP events
    since the last pass, speak the transitions. First run ever: arm at
    NOW (the past's outages are not today's story)."""
    from zcode_bot.cli_logs import iter_events   # lazy: stays patchable
    t_now = now if now is not None else time.time()
    track, since = _load_track()
    if track is None:
        _save_track(ConnTrack(), t_now)
        return []
    last = since
    it = iter_ev or iter_events
    notices: List[tuple] = []
    try:
        for ts, _sid, ev, d in it(config.BASE, needles=NEEDLES,
                                  since_epoch=since or None,
                                  last_files=1, tail_bytes=0):
            reason = str((d.get("context") or {}).get("statusMessage")
                         or "")
            notices.extend(track.feed(ts, ev, reason))
            if ts > last:
                last = ts
    except OSError:
        pass  # silent-by-design: no log yet (the app never ran today)
    new_since = last + 0.5 if last > 0.0 else t_now
    _save_track(track, max(new_since, since))
    for n in notices:
        _notify(cfg, n)
        log("kb", "zconn %s" % n[0])
    return notices
