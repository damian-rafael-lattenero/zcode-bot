"""Logging utilities for zcode_bot."""
import json
import os
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".zcode", "hooks", "state")
LOG_DIR = os.path.join(os.path.expanduser("~"), ".zcode", "hooks", "logs")


def log(kind, msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(LOG_DIR, f"{kind}.log"), "a") as f:
            f.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def log_payload(event, payload):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(LOG_DIR, f"payload-{stamp}-{event}.json"), "w") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    except (OSError, TypeError):
        pass


def pwlog(*a):
    """Log de pasos del bot unificado (worker + entradas del poller)."""
    import datetime
    try:
        p = os.path.join(os.path.expanduser("~"), ".zcode", "hooks", "logs", "prompt-worker.log")
        with open(p, "a") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"),
                                    " ".join(str(x) for x in a)))
    except OSError:
        pass