"""Bot logging — canonical bodies (F1e consolidation: the gateway
defined these same functions and shadowed this module; ONE living
body). Paths by attribute (config.LOG_DIR / config.BASE):
fixture-aware in tests.
"""
import datetime
import json
import os
import time

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
        pass


def log_payload(event, payload):
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(config.LOG_DIR, f"payload-{stamp}-{event}.json"),
                  "w") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    except (OSError, TypeError):
        pass


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
        pass


def _log_rotate(path, max_bytes=5 * 1024 * 1024):
    """>5MB → .1 (one generation: the log is diagnostics, not an archive)."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass
