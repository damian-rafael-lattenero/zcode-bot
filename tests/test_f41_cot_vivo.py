"""F41/F43/F46/F57 — the Mac's CoT LIVE: size-based boxes with buttons.

The watchdog polls the history every 3s. F57: every box lives in its OWN
message — born «thinking… filling CoT R.C» at the box's START (text-only),
edited text-only while it fills and FROZEN at the seal («CoT R.C · size»
+ its single button, global index); a frozen message is never touched
again. There are no round messages anymore: the boxes are the timeline.
Boxes that sealed before their message could be born (bulk arrivals
between polls) travel in ONE late message at the close (F49). Damian's
model (F43): a cap per box; F46: «CoT R.C» labels (reasoning block R,
box C — never two «CoT 1»). Base verified live 20/10: the reasoning part
of the step in progress is already readable in session/messages mid-step.
"""
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot.utils import http as zh  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
# F51c: recent — the long-turn notice fires with an old t0 and the
# loop tests would blow up with an extra ⏱️ inside
# F51c: recent (120s ago: far from the real marker's second of
# turn 2 in the pickups, far from the notice threshold)
T0 = int(time.time()) - 120
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
CAP = reasoning.COT_BOX_CAP


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
        boxes, labels, pending, k = reasoning.live_boxes(msgs)
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
        boxes, labels, pending, k = reasoning.live_boxes(msgs)
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
        support.patch(self, reasoning.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((text, markup)), True)[1])
        self.naps = []
        support.patch(self, reasoning.time, "sleep", lambda s: self.naps.append(s))
        with open(reasoning.marker_path(SID), "w") as f:
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

        proto = _ProtoSilent(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make",
                        lambda cwd=None, **kw: proto)
        t0 = int(time.time()) - 10
        support.patch(self, reasoning.config, "LONG_TURN_NOTICE_S", 0)
        reasoning.run_watcher(CFG, SID, "cv", t0, "/w")
        longs = [t for t, _mk in self.sends
                 if "has been running" in t and "min on the Mac" in t]
        self.assertEqual(len(longs), 1)  # once: gap ≥60s even though the
        # patched threshold is 0 (dedupe by next, not by loop)
        self.assertIn("«cv»", longs[0])
        self.assertIn("app's Stop", longs[0])

    def test_each_box_message_born_at_its_start_and_frozen_at_its_seal(self):
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        # F57: every box lives in its OWN message (fast turns close
        # before the 45 s pipe beat fires — its own test covers it).
        n0, mk0 = self.sends[0]
        self.assertIn("filling CoT 1.2 (500 chars)", n0)
        self.assertIsNone(mk0)                    # born WITHOUT keyboard
        # poll 2 seals 1.2: its message FREEZES with ITS single button
        # (global index 2)… and 2.1's own message is born below, text-only
        self.assertIn("filling CoT 2.1 (300 chars)", self.sends[1][0])
        self.assertIsNone(self.sends[1][1])
        t12, k12 = self.edits[0]
        self.assertIn("<b>CoT 1.2</b>", t12)
        self.assertEqual([b["text"] for f in k12["inline_keyboard"] for b in f],
                         ["CoT 1.2"])
        self.assertEqual(k12["inline_keyboard"][0][0]["callback_data"].split(":")[-1],
                         "2")  # its OWN global index
        # 3) the close (F49/F57): 2.1 — sealed in the last poll — freezes
        #    with ITS button; 1.1 — never born — travels in ONE late
        #    message. No round messages: the boxes are the timeline.
        t21, k21 = self.edits[1]
        self.assertIn("<b>CoT 2.1</b>", t21)
        self.assertEqual([b["text"] for f in k21["inline_keyboard"] for b in f],
                         ["CoT 2.1"])
        late, klate = next(s for s in self.sends if "as the turn closed" in s[0])
        self.assertEqual([b["text"] for f in klate["inline_keyboard"] for b in f],
                         ["CoT 1.1"])
        self.assertEqual(len(self.edits), 2)   # nothing else is ever edited
        self.assertFalse(any("pass " in t for t, _m in self.edits))
        self.assertFalse(any("cut" in t for t, _m in self.edits))
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(d["phs"], [777, 777, 777])  # F57: ph per box
        self.assertTrue(d["done"])
        self.assertTrue(proto.closed)
        self.assertTrue(all(s == reasoning.COT_LIVE_POLL_S for s in self.naps))
        # the tap on the 3rd box (2nd reasoning block) opens ITS text with ITS label
        out = reasoning.show(SID, T0, 3)
        self.assertIn("<pre>%s</pre>" % ("b" * 300), out)
        self.assertIn("<b>CoT 2.1</b>", out)

    def test_a_task_switch_mutes_the_turns_stream(self):
        """F71 — the incident (26/10): the user moved to the diff_ask
        task while the zcode_bot Mac turn kept running, and its boxes
        and beats kept landing over it. The F15 gate is read LIVE on
        every push now (before: spawn-time only); the boxes still seal
        in the cache (/cot of the task recovers them)."""
        from zcode_bot import core as zc_core
        zc_core.set_active_bot_session({"sid": "sess_other", "title": "o",
                                        "ws": "/w"})
        support.patch(self, reasoning, "MAC_BEAT_S", 0)
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(dict(CFG, only_active_task=True), SID, "cv",
                              T0, "/w")
        self.assertEqual(self.sends, [])
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertTrue(d["done"])            # the turn closed, cached
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])

    def test_attach_mid_turn_resumes_at_once(self):
        """F72 — the /task pick of a Mac turn that kept running muted
        (F71): the beat fires the SAME round and the boxes sealed through
        the muted gap land as ONE collapsed bundle — not at the close.
        Damian, 26/10: «no comienza a streamear al instante»."""  # noqa: spanish — Damian's report, quoted
        ok = [True]
        support.patch(self, reasoning, "_task_current_ok",
                      lambda cfg, sid: ok[0])

        class _ProtoFlip(_ProtoCreciente):
            # the gate flips with the polls: live → switched away →
            # attached again (the third poll IS the /task pick)
            def request(self, method, params=None, timeout=None):
                r = super().request(method, params, timeout)
                if method == "session/messages":
                    ok[0] = self.seen != 1
                return r

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoFlip(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(dict(CFG, only_active_task=True), SID, "cv",
                              T0, "/w")
        # the resume beat, with real material, on the re-attach round
        beats = [t for t, _mk in self.sends if "boxes sealed" in t]
        self.assertTrue(beats, self.sends)
        self.assertIn("2 boxes sealed", beats[0])
        # the gap's boxes: ONE collapsed bundle (1.2 kept its live
        # message; 1.1 and 2.1 never materialized)
        bundles = [s for s in self.sends if "sealed while you were away" in s[0]]
        self.assertEqual(len(bundles), 1)
        self.assertEqual([b["text"] for row in bundles[0][1]["inline_keyboard"]
                          for b in row], ["CoT 1.1", "CoT 2.1"])
        # nothing left for the close-time latecomers — the cache is whole
        self.assertFalse([s for s in self.sends if "as the turn closed" in s[0]])
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])
        self.assertTrue(all(d["phs"]))
        self.assertTrue(d["done"])

    def test_attach_respawn_inherits_messages_and_bundles_the_rest(self):
        """F72 — the attach respawns a DEAD cot watcher mid-turn (a
        deploy killed it): the FIRST sync lands the boxes the chat never
        saw at once, and the predecessor's frozen messages are NOT
        re-sent (its cache seeds the phs)."""
        reasoning.save_cache(SID, T0, "cv", ["x" * CAP], ["CoT 1.1"],
                             phs=[555], perbox=True, live=True)
        support.patch(self, reasoning, "MAC_BEAT_S", 10 ** 6)  # beats out

        class _ProtoFixed:
            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 2:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": [
                        _msg("user", {"type": "text", "text": "p"}),
                        _msg("assistant", _r("a" * (CAP + 500)),
                             {"type": "text", "text": "end"})]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoFixed(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w", catchup=True)
        bundles = [s for s in self.sends if "sealed while you were away" in s[0]]
        self.assertEqual(len(bundles), 1)
        self.assertEqual([b["text"] for row in bundles[0][1]["inline_keyboard"]
                          for b in row], ["CoT 1.2"])  # 1.1 is in the chat
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["phs"], [555, 777])   # inherited + bundled
        self.assertTrue(d["done"])

    def test_the_pipe_beat_carries_real_activity(self):
        # F70 — Damian's base requirement: the pipe visible, circulating
        # at every moment. A slow reasoning turn gets a beat with the
        # in-flight size, the steps and the sealed-box counter; a plain
        # text turn (nothing to report) stays silent (f45 covers that).
        support.patch(self, reasoning, "MAC_BEAT_S", 0)
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        beats = [t for t, _ in self.sends if "step" in t and "min" in t]
        self.assertTrue(beats, self.sends)
        self.assertIn("thinking", beats[0])
        self.assertIn("sealed", beats[0])

    def test_agent_view_mutes_the_live_stream(self):
        # F67 — while one of the session's agents is on VIEW, the parent's
        # live boxes hold (picking a subagent = only that subagent
        # streams); the boxes still seal in the ledger and the closing
        # bundle travels at the end. F70: the pipe beat holds too — the
        # agent's view owns the phone; the ONE message is the bundle.
        from zcode_bot import agent_live
        with open(agent_live.view_pid_path(SID), "w") as f:
            f.write("1234")
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        self.assertFalse([t for t, _ in self.sends if "filling" in t])
        self.assertFalse([t for t, _ in self.sends if "in-flight" in t])
        self.assertEqual(len(self.sends), 1)   # just the closing bundle
        self.assertEqual(self.edits, [])

    def test_failed_resume_leaves_the_way_to_f40(self):
        proto = _ProtoCreciente(reasoning.marker_path(SID))

        def req(method, params=None, timeout=None):
            if method == "session/resume":
                return {"error": {"code": -1, "message": "boom"}}
            return {"result": {"events": []}}
        proto.request = req
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        with open(reasoning.cache_path(SID, T0)) as f:
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
        reasoning.pid_write(SID)
        reasoning.save_cache(SID, t0_live, "cv", [], [], live=True)  # takes ownership
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
