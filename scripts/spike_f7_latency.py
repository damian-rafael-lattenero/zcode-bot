"""spike_f7_latency.py — F7+: measures where the ~85s of turn startup
go (worker start → send accepted) against ONE real session.

Usage: PYTHONPATH=src python3 scripts/spike_f7_latency.py <sessionId>
Sends no prompts: only spawn/resume/setModel/setMode/fetch (read-only).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from zcode_bot import proto as zc_proto  # noqa: E402
from zcode_bot.models import MODEL_SEL  # noqa: E402

SID = sys.argv[1] if len(sys.argv) > 1 else None
if not SID:
    print("usage: spike_f7_latency.py <sessionId>")
    sys.exit(1)
WS = os.path.expanduser("~/Projects/bot")


def paso(nombre, fn):
    t = time.time()
    try:
        r = fn()
        dt = time.time() - t
        res = (r or {}).get("result") or {}
        proj = res.get("projection") or {}
        ctx = "ctx=%s/%s mode=%s" % (proj.get("contextUsed"),
                                     proj.get("contextWindow"),
                                     proj.get("mode")) if proj else ""
        nmsg = len(res.get("messages") or [])
        tam = len(json.dumps(r, default=str))
        print("%-18s %6.1fs  resp=%dKB msgs=%d %s"
              % (nombre, dt, tam // 1024, nmsg, ctx))
        if proj:
            print("                   projection_keys=%s"
                  % sorted(proj.keys()))
        return r
    except Exception as e:  # noqa: BLE001
        print("%-18s FAILED %.1fs  %s" % (nombre, time.time() - t, e))
        return None


t_total = time.time()
c = None
t = time.time()
try:
    c = zc_proto.make(cwd=WS)
    print("%-18s %6.1fs" % ("proto.make", time.time() - t))
except Exception as e:  # noqa: BLE001
    print("proto.make FAILED %s" % e)
if c is not None:
    paso("session/resume", lambda: c.request(
        "session/resume", {"sessionId": SID}, timeout=90))
    paso("session/setModel", lambda: c.request(
        "session/setModel", {"sessionId": SID, "model": MODEL_SEL},
        timeout=60))
    paso("session/setMode", lambda: c.request(
        "session/setMode", {"sessionId": SID, "mode": "yolo"}, timeout=60))
    paso("session/messages(1)", lambda: c.request(
        "session/messages", {"sessionId": SID, "limit": 1}, timeout=60))
    c.close()
print("TOTAL %.1fs" % (time.time() - t_total))
