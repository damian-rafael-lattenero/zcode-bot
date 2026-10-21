"""F41/F43/F46 — the Mac's CoT LIVE: size-based boxes with buttons.

The watchdog polls the history every 3s; boxes already FULL travel as
buttons as soon as they fill (the button box is BORN with the first
one), the one still filling does not travel, and when the turn closes
the final text brings the total. Damian's model (F43): a cap per box.
F46: «CoT R.C» labels (reasoning block R, box C — never two «CoT 1»)
and ONE button box per reasoning block, each born at its chronological
position. Base verified live 20/10: the reasoning part of the step in
progress is already readable in session/messages mid-step.
"""
import json
import os
import sys
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import reasoning as rz  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot.utils import http as zh  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
# F51c: recent — the long-turn notice fires with an old t0 and the
# loop tests would blow up with an extra ⏱️ inside
# F51c: recent (120s ago: far from the real marker's second of
# turn 2 in the pickups, far from the notice threshold)
T0 = int(time.time()) - 120
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
CAP = rz.COT_BOX_CAP


def _msg(role, *parts):
    return {"info": {"role": role}, "parts": list(parts)}


def _r(text):
    return {"type": "reasoning", "text": text}


class TestLiveBoxes(unittest.TestCase):
    def test_the_in_progress_box_does_not_travel_until_full(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("a" * (CAP + 700))),  # in progress: 1 full + remainder
        ]
        boxes, labels, pending, k = rz.live_boxes(msgs)
        self.assertEqual([len(c) for c in boxes], [CAP])
        self.assertEqual(labels, ["CoT 1.1"])
        self.assertEqual((pending, k), (700, "1.2"))

    def test_closed_passes_travel_whole(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("a" * (CAP + 100)), {"type": "tool",
                                                       "tool": "Bash"}),
            _msg("assistant", _r("b" * 50), {"type": "text", "text": "end"}),
        ]
        boxes, labels, pending, k = rz.live_boxes(msgs)
        self.assertEqual([len(c) for c in boxes], [CAP, 100, 50])
        self.assertEqual(labels, ["CoT 1.1", "CoT 1.2", "CoT 2.1"])  # R.C
        self.assertEqual(pending, 0)


class _ProtoCreciente:
    """The history EVOLVES between polls (like the real turn):
    poll 1: one reasoning block in progress with 1 full box;
    poll 2: that block closed (remainder sealed) and another started;
    poll 3 (end): everything complete."""

    def __init__(self, marker):
        self.marker, self.seen, self.closed = marker, 0, False
        A = "a" * (CAP + 500)
        B = "b" * 300
        self.stages = [
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r(B))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r(B), {"type": "text", "text": "end"})],
        ]

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            return {"result": {}}
        if method == "session/messages":
            stage = min(self.seen, len(self.stages) - 1)
            msgs = self.stages[stage]
            self.seen += 1
            if self.seen >= 3:  # F46: one poll sees the 2nd reasoning block closed
                try:
                    os.remove(self.marker)
                except OSError:
                    pass
            return {"result": {"messages": msgs}}
        return {"result": {}}

    def close(self):
        self.closed = True


class TestRunWatcher(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits = [], []
        support.patch(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), 777)[1])
        support.patch(self, rz.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((text, markup)), True)[1])
        self.naps = []
        support.patch(self, rz.time, "sleep", lambda s: self.naps.append(s))
        with open(rz.marker_path(SID), "w") as f:
            f.write("{}")

    def test_long_turn_on_the_mac_announces(self):
        # F51c: a stretched Mac turn notifies ⏱️ with the SAME phrase and
        # the SAME value as the worker (R0b) — the post-box silence of a
        # long turn was read as the bot having frozen (21/10 07:43).
        class _ProtoSilent:
            """No events; on iteration k it consumes the marker (end)."""

            def __init__(self, marker, loops=3):
                self.marker, self.loops, self.n = marker, loops, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > self.loops:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": []}}
                return {"result": {}}

            def close(self):
                pass

        proto = _ProtoSilent(rz.marker_path(SID))
        support.patch(self, rz.zc_proto, "make",
                        lambda cwd=None, **kw: proto)
        t0 = int(time.time()) - 10
        support.patch(self, rz.config, "LONG_TURN_NOTICE_S", 0)
        rz.run_vigilante(CFG, SID, "cv", t0, "/w")
        longs = [t for t, _mk in self.sends
                 if "has been running" in t and "min on the Mac" in t]
        self.assertEqual(len(longs), 1)  # once: gap ≥60s even though the
        # patched threshold is 0 (dedupe by next, not by loop)
        self.assertIn("«cv»", longs[0])
        self.assertIn("app's Stop", longs[0])

    def test_box_born_with_the_first_full_one_and_grows(self):
        proto = _ProtoCreciente(rz.marker_path(SID))
        support.patch(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.run_vigilante(CFG, SID, "cv", T0, "/w")
        # 1) BORN with the first box FULL (no empty "thinking" message):
        #    1 button CoT 1.1, announcing the box being filled
        n0, mk0 = self.sends[0]
        b0 = [b for f in mk0["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b0], ["CoT 1.1"])
        self.assertIn("filling CoT 1.2 (500 chars)", n0)
        # 2) reasoning block 1 grew: edit with ITS 2 buttons (1.1 and 1.2)
        n1, mk1 = self.edits[0]
        b1 = [b for f in mk1["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b1], ["CoT 1.1", "CoT 1.2"])
        # 3) F46: the 2nd reasoning block closed → ITS message keeps the final state…
        nc, mkc = self.edits[1]
        bc = [b for f in mkc["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in bc], ["CoT 1.1", "CoT 1.2"])
        self.assertIn("pass 1", nc)
        #    …and the 2nd is born in a NEW message with ITS single button
        self.assertEqual(len(self.sends), 2)
        n2, mk2 = self.sends[1]
        b2 = [b for f in mk2["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b2], ["CoT 2.1"])
        # 4) final (F49): the LAST reasoning block closes like the rest —
        #    ITS message, ITS buttons; no index with all of them piled up
        #    at the end. Cache finalized with phs per box.
        nf, mkf = self.edits[-1]
        bf = [b for f in mkf["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in bf], ["CoT 2.1"])
        self.assertIn("pass 2", nf)
        self.assertIn("1 box", nf)
        self.assertFalse(any("Reasoning" in t for t, _m in self.edits))
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(d["phs"], [777, 777, 777])  # F46: ph per box
        self.assertTrue(d["done"])
        self.assertTrue(proto.closed)
        self.assertTrue(all(s == rz.COT_LIVE_POLL_S for s in self.naps))
        # the tap on the 3rd box (2nd reasoning block) opens ITS text with ITS label
        out = rz.show(SID, T0, 3)
        self.assertIn("<pre>%s</pre>" % ("b" * 300), out)
        self.assertIn("<b>CoT 2.1</b>", out)

    def test_failed_resume_leaves_the_way_to_f40(self):
        proto = _ProtoCreciente(rz.marker_path(SID))

        def req(method, params=None, timeout=None):
            if method == "session/resume":
                return {"error": {"code": -1, "message": "boom"}}
            return {"result": {"events": []}}
        proto.request = req
        support.patch(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.run_vigilante(CFG, SID, "cv", T0, "/w")
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertTrue(d.get("done"))  # does not stay hung as the owner
        self.assertEqual(d["boxes"], [])


class TestSpawns(unittest.TestCase):
    """on_user_prompt spawns --cot-live; on_stop does NOT spawn
    --cot-buttons when the watchdog already owns it (cache exists)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.patch(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        support.patch(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None: True)
        self.zt.set_active_bot_session(
            {"sid": SID, "title": "cv", "ws": "/w"})
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        notify_prompt=True, mac_cot_buttons=True)

    def test_prompt_spawns_cot_live(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": SID, "cwd": "/w",
                                          "title": "cv", "prompt": "p"})
        live = [a for a in self.spawns if "--cot-live" in a]
        self.assertEqual(len(live), 1)
        i = live[0].index("--cot-live")
        self.assertEqual(live[0][i + 1], SID)
        self.assertEqual(live[0][i + 3], str(int(os.path.getmtime(
            self.zt.marker_path(SID)))))

    def test_knob_off_does_not_spawn(self):
        self.zt.on_user_prompt(dict(self.cfg, mac_cot_buttons=False),
                               {"sessionId": SID, "prompt": "p"})
        self.assertEqual([a for a in self.spawns if "--cot-live" in a], [])

    def _stop(self):
        self.zt.on_stop(dict(self.cfg, notify_stop=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})

    def test_on_stop_skips_f40_if_the_live_one_owns_it(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": SID, "cwd": "/w",
                                          "title": "cv", "prompt": "p"})
        live = next(a for a in self.spawns if "--cot-live" in a)
        t0_live = int(live[live.index("--cot-live") + 3])
        # F51a: ownership by the live watcher = its pid-file (live
        # process) + the live=True cache it wrote — even if it is
        # summarizing (heartbeat expired), on_stop does not steal the
        # closing.
        rz.pid_write(SID)
        rz.save_cache(SID, t0_live, "cv", [], [], live=True)  # takes ownership
        self._stop()
        self.assertEqual([a for a in self.spawns if "--cot-buttons" in a], [])

    def test_on_stop_launches_f40_if_there_is_no_cache(self):
        with open(self.zt.marker_path(SID), "w") as f:
            f.write("{}")
        self._stop()
        self.assertEqual(len([a for a in self.spawns
                              if "--cot-buttons" in a]), 1)


if __name__ == "__main__":
    unittest.main()
