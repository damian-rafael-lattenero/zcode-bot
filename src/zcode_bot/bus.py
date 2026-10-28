"""bus — publisher of the agent-brain event bus (ADR-019). Standard library only.

One JSON line per event appended to config.BUS_FILE. Contract, consumed by
agent-brain's tailer: {"id","ts","kind","sid","turn","source","payload"}.

Invariants, each one a test in tests/test_bus.py:
  * the bus NEVER breaks a turn: tap() swallows every error, counts it in
    `failures` and returns False (the live-io invariant);
  * off by default: cfg["event_bus"] gates every write, so a bot without
    the knob behaves byte-for-byte as before;
  * at-least-once is the consumer's problem: ids are unique (uuid4) and
    the consumer deduplicates by id;
  * one owner per turn: the worker publishes its phone turns, the hooks
    publish the Mac's turns only when no worker owns the session;
  * lines stay small: free-text fields are clipped to PAYLOAD_TEXT_CAP so
    one append is one write (no torn lines between processes).
"""
import json
import os
import threading
import time
import uuid

from zcode_bot import config

KINDS = ("user.prompt", "turn.started", "tool.call", "tool.result",
         "turn.completed", "turn.failed")
PAYLOAD_TEXT_CAP = 4000
MS = 1000
_lock = threading.Lock()
failures = 0  # in-process counter, never reset


def enabled(cfg):
    return bool(cfg.get("event_bus"))


def clip(text):
    s = "" if text is None else str(text)
    return s if len(s) <= PAYLOAD_TEXT_CAP else s[:PAYLOAD_TEXT_CAP] + "…"


def turn_id(t0):
    """The turn's id within its session: ms epoch of the send. Every tap
    of one turn holds the same t0, so they all agree without a registry."""
    return "%d" % int(float(t0) * MS)


def _now_iso():
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % (int(t * MS) % MS)


def event(kind, sid, payload=None, turn="", source="bot"):
    if kind not in KINDS:
        raise ValueError("unknown bus event kind: %r" % (kind,))
    return {"id": uuid.uuid4().hex, "ts": _now_iso(), "kind": kind,
            "sid": str(sid or ""), "turn": str(turn or ""), "source": source,
            "payload": payload or {}}


def publish(cfg, kind, sid, payload=None, turn="", source="bot"):
    """Appends one event. True when written; False when the knob is off or
    the write failed (counted). Raises only on a programmer error (unknown
    kind), which the suite catches before production does."""
    global failures
    if not enabled(cfg):
        return False
    line = json.dumps(event(kind, sid, payload, turn, source),
                      ensure_ascii=False, default=str) + "\n"
    try:
        os.makedirs(os.path.dirname(config.BUS_FILE), exist_ok=True)
        with _lock:
            with open(config.BUS_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        return True
    except OSError:
        failures += 1
        return False


def tap(cfg, kind, sid, payload=None, turn="", source="bot"):
    """The tap points call THIS: publish wrapped so that nothing, not even a
    bug in this module, can escape into a turn."""
    global failures
    try:
        return publish(cfg, kind, sid, payload, turn, source)
    except Exception:  # noqa: BLE001 - the bus never breaks a turn  # silent-by-design: deliberate degradation
        failures += 1
        return False
