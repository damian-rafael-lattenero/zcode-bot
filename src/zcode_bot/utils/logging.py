"""Bot logging — canonical bodies (F1e consolidation: the gateway
defined these same functions and shadowed this module; ONE living
body). Paths by attribute (config.LOG_DIR / config.BASE):
fixture-aware in tests.

F79a adds the two anti-silent-death primitives:
- log_exc(kind, msg): the exception-wall engine — msg plus the FULL
  traceback of the exception being handled (call it inside except).
  Until F79b's leveled engine lands, this is the one body every broad
  wall funnels through: a wall that logs only str(exc) was exactly how
  the 14:25:48 worker died leaving no trace of WHY.
- hb(name): the per-cycle heartbeat — an mtime touch in state/. A loop
  that beats every cycle (even idle) and a dead loop become
  distinguishable by lstat alone; the healer (F79c) reads these ages.
"""
import datetime
import json
import os
import sys
import time
import traceback

from zcode_bot import config


def log(kind, msg):
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        p = os.path.join(config.LOG_DIR, f"{kind}.log")
        # rotate-on-write (same treatment as pwlog): housekeeping only
        # runs when the poller starts, and a flood within ONE run
        # already hit 294MB with no cap (19/10 incident, 3.1M tracebacks)
        _log_rotate(p)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a") as f:
            f.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass  # silent-by-design: logging must never kill the caller


def log_exc(kind, msg):
    """The exception wall (F79a): msg + the FULL traceback of the
    exception currently being handled. Never truncated — the [-500:]
    crash logs of 27/10 amputated exactly the frames that named the
    bug. Call inside the except block."""
    _, _exc, tb = sys.exc_info()
    if tb is None:
        log(kind, msg)
        return
    log(kind, "%s\n%s" % (msg, "".join(
        traceback.format_exception(_, _exc, tb)).rstrip()))


def hb(name):
    """Per-cycle heartbeat (F79a): touch state/hb-<name>. The mtime IS
    the beat — the healer distinguishes an idle-but-alive loop (fresh
    mtime) from a dead one (stale) by lstat alone. Best-effort: a
    heartbeat failure must never kill the loop it measures."""
    try:
        p = os.path.join(config.STATE_DIR, "hb-%s" % name)
        with open(p, "a"):
            os.utime(p, None)
    except OSError:
        pass  # silent-by-design: diagnostics beat, never fatal


def log_payload(event, payload):
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(config.LOG_DIR, f"payload-{stamp}-{event}.json"),
                  "w") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    except (OSError, TypeError):
        pass  # silent-by-design: cache/state write, best-effort


def pwlog(*a):
    """Unified bot step log (worker + poller entries). F24: with a DATE
    (the 13/10 forensic reconstruction required crossing 3 sources to
    date lines) and size-based rotation."""
    try:
        p = os.path.join(config.BASE, "hooks", "logs", "prompt-worker.log")
        _log_rotate(p)
        with open(p, "a") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now()
                                   .strftime("%m-%d %H:%M:%S"),
                                   " ".join(str(x) for x in a)))
    except OSError:
        pass  # silent-by-design: cache/state write, best-effort


def _log_rotate(path, max_bytes=5 * 1024 * 1024):
    """>5MB → .1 (one generation: the log is diagnostics, not an archive)."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass  # silent-by-design: cache/state write, best-effort