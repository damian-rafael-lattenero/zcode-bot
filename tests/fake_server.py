#!/usr/bin/env python3
"""FAKE stdio server for ProtoClient tests.

Like the real app-server, it serves requests CONCURRENTLY (one thread
per request): a slow request doesn't block the ones behind it — a
property /stop needs (stop on top of an in-flight session/send).
Responds {"id":..,"result":{"echo":method}}; "slow_ms" simulates
latency.
"""
import json
import os
import sys
import threading
import time

sys.stdout_lock = threading.Lock()


def atender(req):
    slow = (req.get("params") or {}).get("slow_ms") or 0
    if slow:
        mark = os.environ.get("ZCFAKE_SLOW_MARK")
        if mark:
            # signal for the test: the slow request is ALREADY in flight here
            with open(mark, "w") as f:
                f.write(req.get("method") or "?")
        time.sleep(slow / 1000.0)
    pad = (req.get("params") or {}).get("pad_bytes") or 0
    result = {"echo": req.get("method")}
    if pad:
        result["pad"] = "x" * int(pad)  # BIG frame: max_frame test
    frame = json.dumps({"id": req.get("id"), "result": result})
    # no trailing \n: forces the reader's 512KB fallback (test F13)
    sep = "" if (req.get("params") or {}).get("no_newline") else "\n"
    with sys.stdout_lock:
        sys.stdout.write(frame + sep)
        sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except ValueError:
        continue
    threading.Thread(target=atender, args=(req,), daemon=True).start()
