"""spike_f9_turnfin.py — F9: does the event stream carry the turn end?

Decision gate of the follow-up declared in ADR-013: `_turn_end_poll`
today scans the CLI's JSONL logs (turn.started/completed/failed with
turnId). To replace it with events I need to know whether the
`session/events` stream exposes the TURN lifecycle and with which
correlation keys (turnId / seq / kind).

PROBE MODE (default): read-only over an existing session — resume +
subscribe + session/events dumps WITHOUT sends (the session's recent
turns leave their events in the buffer).
TURN MODE (--turn): throwaway session in /tmp + minimal turn (what
spike_f7a used to do) to watch turn.* live — only if the probe is not
enough.

Usage: PYTHONPATH=src python3 scripts/spike_f9_turnfin.py [sid] [--turn]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from zcode_bot import proto as zc_proto  # noqa: E402
from zcode_bot.models import MODEL_SEL  # noqa: E402

WS = "/tmp/f9-ws"
os.makedirs(WS, exist_ok=True)


def corto(obj, n=400):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…(+%d)" % (len(s) - n)


def histograma(evs, vistos, hist, crudo, keys):
    for ev in evs:
        eid = ev.get("eventId")
        if eid in vistos:
            continue
        vistos.add(eid)
        pl = ev.get("payload") or {}
        k = "%s.%s" % (ev.get("type") or "?", pl.get("kind", "-"))
        hist[k] = hist.get(k, 0) + 1
        crudo.setdefault(k, ev)
        keys.setdefault(k, sorted(set(keys.get(k, [])) | set(pl.keys())))


def main():
    modo_turn = "--turn" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    sid = args[0] if args else None
    c = zc_proto.make(cwd=WS)
    try:
        if not sid:
            r = c.request("session/create",
                          {"workspace": {"workspaceKey": WS,
                                         "workspacePath": WS}}, timeout=60)
            sid = ((r.get("result") or {}).get("session") or {}).get(
                "sessionId")
            print("new SID:", sid)
        else:
            r = c.request("session/resume", {"sessionId": sid}, timeout=120)
            print("resume ->", corto(r, 150))
        r = c.request("session/subscribe",
                      {"sessionId": sid, "deliveryKind": "desktop-continuous"},
                      timeout=30)
        print("subscribe ->", corto(r, 200))
        seq0 = ((r.get("result") or {}).get("eventSeq"))
        print("cursor eventSeq:", seq0)

        if modo_turn:
            c.request("session/setModel", {"sessionId": sid,
                                           "model": MODEL_SEL}, timeout=30)
            t_send = time.time()
            c.request("session/send",
                      {"sessionId": sid,
                       "content": "Reply only: ok",
                       "modelSelection": MODEL_SEL}, timeout=120)
            print("send accepted, polling events…")

        hist, crudo, keys = {}, {}, {}
        vistos = set()
        last_seq = None  # None = FIRST poll without a cursor (buffer replay)
        t0 = time.time()
        while time.time() - t0 < (90 if modo_turn else 15):
            time.sleep(2)
            params = {"sessionId": sid}
            if last_seq is not None:
                params["afterSeq"] = last_seq
            r = c.request("session/events", params, timeout=20)
            evs = (r.get("result") or {}).get("events") or []
            for ev in evs:
                if isinstance(ev.get("seq"), int) and \
                        (last_seq is None or ev["seq"] > last_seq):
                    last_seq = ev["seq"]
            histograma(evs, vistos, hist, crudo, keys)
            hay_fin = any(k.split(".")[0] in ("turn.completed", "turn.failed")
                          for k in hist)
            if modo_turn and hay_fin:
                break

        print("\n=== HISTOGRAM type.payload_kind (%d unique, last_seq=%s) ==="
              % (len(vistos), last_seq))
        for k in sorted(hist, key=lambda k: -hist[k]):
            print("%5d  %-44s payload_keys=%s" % (hist[k], k, keys[k]))
        print("\n=== RAW dump of turn-end candidates ===")
        for k in sorted(crudo):
            if "turn" in k or "permission" in k or "compact" in k:
                print(k, "->", corto(crudo[k], 700))
    finally:
        c.close()


if __name__ == "__main__":
    main()
