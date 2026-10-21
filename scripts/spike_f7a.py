"""spike_f7a.py — F7a: dumps the REAL session/events frame.

Decision gate of the F7 plan (live action): confirms (1) the real shape
of each event (zcode.cjs schema: {sequence, type, payload} vs fixtures
{eventId, payload:{kind}}), (2) that tool_input_* DO travel in the
poll, (3) that afterSeq returns incremental data, (4) whether
session/messages exposes the tool call id in the part.

Usage: PYTHONPATH=src python3 scripts/spike_f7a.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from zcode_bot import proto as zc_proto  # noqa: E402
from zcode_bot.models import MODEL_SEL  # noqa: E402

WS = "/tmp/f7a-ws"
os.makedirs(WS, exist_ok=True)


def corto(obj, n=600):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…(+%d)" % (len(s) - n)


c = zc_proto.make(cwd=WS)
try:
    r = c.request("session/create",
                  {"workspace": {"workspaceKey": WS, "workspacePath": WS}},
                  timeout=60)
    sid = ((r.get("result") or {}).get("session") or {}).get("sessionId")
    print("SID:", sid)
    for m, p in (("session/setModel", {"sessionId": sid, "model": MODEL_SEL}),
                 ("session/setMode", {"sessionId": sid, "mode": "yolo"}),
                 ("session/subscribe", {"sessionId": sid,
                                        "deliveryKind": "desktop-continuous"})):
        r = c.request(m, p, timeout=30)
        print(m, "->", corto(r, 200))

    c.request("session/send",
              {"sessionId": sid,
               "content": "Run `echo hola-f7` with the Bash tool and "
                          "report the output. Nothing else.",
               "modelSelection": MODEL_SEL}, timeout=120)

    hist, crudo, keys, seqs = {}, {}, {}, set()
    vistos = set()
    t0 = time.time()
    while time.time() - t0 < 100:
        time.sleep(2)
        r = c.request("session/events", {"sessionId": sid}, timeout=20)
        evs = (r.get("result") or {}).get("events") or []
        for ev in evs:
            eid = ev.get("eventId")
            if eid in vistos:
                continue
            vistos.add(eid)
            pl = ev.get("payload") or {}
            k = "%s.%s" % (ev.get("type") or "?", pl.get("kind", "-"))
            hist[k] = hist.get(k, 0) + 1
            crudo.setdefault(k, ev)
            keys.setdefault(k, sorted(pl.keys()))
            if isinstance(ev.get("seq"), int):
                seqs.add(ev["seq"])
        if "turn.completed" in [k.split(".")[0] for k in hist] or \
                "turn.failed" in hist:
            break

    print("\n=== HISTOGRAM type.payload_kind (dedupe eventId, %d unique) ==="
          % len(vistos))
    for k in sorted(hist, key=lambda k: -hist[k]):
        print("%5d  %-40s payload_keys=%s" % (hist[k], k, keys[k]))
    print("seqs: min=%s max=%s n=%s" %
          (min(seqs) if seqs else None, max(seqs) if seqs else None, len(seqs)))

    print("\n=== RAW (first event of each kind, cap 700) ===")
    for k in sorted(crudo):
        print("--- %s\n%s" % (k, corto(crudo[k], 700)))

    if seqs:
        time.sleep(1)
        r = c.request("session/events",
                      {"sessionId": sid, "afterSeq": max(seqs)}, timeout=20)
        evs2 = (r.get("result") or {}).get("events") or []
        print("\n=== afterSeq=%s → %d events (0-2 = incremental OK) ===" %
              (max(seqs), len(evs2)))
        for ev in evs2[:3]:
            print(corto(ev, 300))

    r = c.request("session/messages", {"sessionId": sid, "limit": 50},
                  timeout=30)
    msgs = r.get("result") or []
    if isinstance(msgs, dict):
        msgs = msgs.get("messages") or msgs.get("items") or []
    print("\n=== session/messages: %d messages ===" % len(msgs))
    for m in msgs:
        for p in (m.get("parts") or []):
            if p.get("type") == "tool":
                print("TOOL PART keys=%s\n%s" % (sorted(p.keys()),
                                                 corto(p, 700)))
finally:
    c.close()
