"""F45 — a turn that closes WITHOUT reasoning says so (before: silence).

Diagnosis from 20/10 on the raw runtime rollout: the bot requests
IDENTICAL thinking for GLM-5.3 and Flash (thinking enabled + effort
max); GLM-5.3 reasons ADAPTIVELY — it decides per call — and in the
bot's session it went 12 calls in a row without reasoning with ~130k
tokens of context (it came back after compacting); Flash reasons
almost always. Zero boxes and no message whatsoever was
indistinguishable from a bug: now all three sources send ONE
«no CoT» line (knob cot_aviso_vacio). capture() is covered in
test_f40.
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
from zcode_bot.live_view import LiveTurnView  # noqa: E402

SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
# F51c: recent — the long-turn notice fires with an old t0
# F51c: recent (120s ago: far from the seconds of the real
# marker of turn 2 in the pickups, far from the notice threshold)
T0 = int(time.time()) - 120


# ------------------------------------------------------------- phone (F42)

class _FakeC:
    def __init__(self, evs):
        self.evs = evs
        self.requests = []

    def request(self, method, params=None, timeout=None):
        self.requests.append(method)
        return {"result": {"events": [dict(e) for e in self.evs]}}


class _Th:
    def __init__(self, n=2):
        self.n = n

    def is_alive(self):
        self.n -= 1
        return self.n > 0


def _ev(eid, kind, delta=""):
    p = {"kind": kind}
    if delta:
        p["delta"] = delta
    return {"eventId": eid, "payload": p}


class TestCelu(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _view(self, evs, cfg_extra=None, deps_extra=None):
        self.sends = []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append((text, markup)), 601)[1],
            "live_send": lambda cfg, text: 620,
            "live_edit": lambda cfg, mid, text, markup=None: True,
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: None,
        }
        deps.update(deps_extra or {})
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1, cot_buttons=True)
        cfg.update(cfg_extra or {})
        return LiveTurnView(_FakeC(evs), cfg, support.T1, float(T0), "testing",
                            777, _Th(2), last_id=None, deps=deps)

    def _notices(self):
        return [s for s in self.sends if "no CoT" in s[0]]

    async def test_turn_without_reasoning_announces_in_one_line(self):
        v = self._view([_ev(1, "text_delta", "direct reply")])
        v.run()
        self.assertEqual(len(self._notices()), 1)
        text, markup = self._notices()[0]
        self.assertIn("«testing»", text)
        self.assertIsNone(markup)  # one line: no keyboard
        # no boxes means no cache: nothing to touch, nothing to purge
        self.assertFalse(os.path.exists(rz.cache_path(support.T1, T0)))

    async def test_knob_off_stays_silent(self):
        v = self._view([_ev(1, "text_delta", "directo")],
                       cfg_extra={"cot_empty_notice": False})
        v.run()
        self.assertEqual(self._notices(), [])

    async def test_without_deltas_it_is_a_mac_turn_and_stays_silent(self):
        """Without a single delta the turn was run by the Mac app (its
        stream never crosses to the bot): whether it reasoned is
        unknown — stay silent."""
        v = self._view([])
        v.run()
        self.assertEqual(self._notices(), [])

    async def test_user_stop_stays_silent(self):
        v = self._view([_ev(1, "text_delta", "a medias")],
                       deps_extra={"stop_requested": lambda sid: True})
        v.run()
        self.assertEqual(v.stop_requests, 1)
        self.assertEqual(self._notices(), [])

    async def test_with_reasoning_no_notice(self):
        v = self._view([_ev(1, "reasoning_delta", "thinking"),
                        _ev(2, "text_delta", "resp")])
        v.run()
        self.assertEqual(v.cot_boxes, ["thinking"])
        self.assertEqual(self._notices(), [])


# ------------------------------------------------------------- Mac (F41/F44)

def _msg(role, *parts):
    return {"info": {"role": role}, "parts": list(parts)}


NO_REASON = [_msg("user", {"type": "text", "text": "ok?"}),
             _msg("assistant", {"type": "text", "text": "ok."})]


class _ProtoScript:
    """session/messages returns the next step: (msgs, action) with the
    hook's action on the marker — "rm" (on_stop: it closed) or ("new",
    tok) (a new prompt that cuts the turn). Once the script is exhausted
    it repeats the last history (the closing fetch arrives AFTER the
    rm)."""

    def __init__(self, marker, steps):
        self.marker, self.steps, self.closed = marker, list(steps), False
        self.last_msgs = []

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            return {"result": {}}
        if method == "session/messages":
            if self.steps:
                msgs, action = self.steps.pop(0)
                self.last_msgs = msgs
            else:
                msgs, action = self.last_msgs, "rm"
            if action == "rm":
                try:
                    os.remove(self.marker)
                except OSError:
                    pass
            elif action:
                with open(self.marker, "w") as f:
                    json.dump({"token": action[1]}, f)
            return {"result": {"messages": msgs}}
        return {"result": {}}

    def close(self):
        self.closed = True


class TestMacLive(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits = [], []
        support.patch(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), 777)[1])
        support.patch(self, rz.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((ph, text, markup)), True)[1])
        support.patch(self, rz.time, "sleep", lambda s: None)
        with open(rz.marker_path(SID), "w") as f:
            json.dump({"token": "tok1"}, f)  # with a token: another token = cut
        self.cfg = {"token": "T", "chat_id": 1, "cot_empty_notice": True}

    def _run(self, steps, cfg=None):
        proto = _ProtoScript(rz.marker_path(SID), steps)
        support.patch(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.run_vigilante(cfg or self.cfg, SID, "cv", T0, "/w")
        return proto

    def _no_cot(self):
        return [t for _p, t, _m in self.edits if "no CoT" in t]

    def test_the_live_box_ends_up_saying_no_cot(self):
        proto = self._run([(NO_REASON, None), (NO_REASON, "rm")])
        # the box was born on the first poll (0 boxes)…
        self.assertEqual(len(self.sends), 1)
        self.assertIn("0 boxes", self.sends[0][0])
        # …and at close it says «no CoT» (before: «0 boxes, 0 chars. Tap
        # one to read it» without a single button)
        ph, text, markup = self.edits[-1]
        self.assertEqual(ph, 777)
        self.assertIn("no CoT", text)
        self.assertIn("«cv»", text)
        self.assertIsNone(markup)
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["boxes"], [])
        self.assertTrue(d["done"])
        self.assertTrue(proto.closed)

    def test_knob_off_keeps_the_usual_close(self):
        self._run([(NO_REASON, "rm")],
                  cfg={"token": "T", "chat_id": 1, "cot_empty_notice": False})
        self.assertEqual(self._no_cot(), [])
        self.assertIn("Reasoning", self.edits[-1][1])

    def test_without_a_live_box_one_line_goes_out(self):
        """The live box was never born (send returned no id): the notice
        goes out as its own line, with no keyboard."""
        support.patch(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), False)[1])
        self._run([(NO_REASON, "rm")])
        notices = [s for s in self.sends if "no CoT" in s[0]]
        self.assertEqual(len(notices), 1)
        self.assertIsNone(notices[0][1])
        self.assertEqual(self.edits, [])

    def test_foreign_cut_asserts_nothing(self):
        """The marker was replaced by a new prompt mid-way through turn 1:
        what was seen live is all there is and whether it reasoned is
        NOT known → turn 1 closes as always; turn 2 (chained, closed
        cleanly without reasoning) does give the notice."""
        self._run([(NO_REASON, ("new", "tok2")), (NO_REASON, "rm")])
        self.assertEqual(len(self._no_cot()), 1)
        self.assertIn("Reasoning", self.edits[0][1])  # turn 1: legacy close
        self.assertIn("no CoT", self.edits[-1][1])      # turn 2: the notice

    def test_empty_history_stays_silent(self):
        """It closed but the history brought nothing back (failed fetch /
        lagging store): unknown → neither a notice nor an invented
        close."""
        self._run([([], "rm")])
        self.assertEqual(self._no_cot(), [])
        self.assertFalse(any("no CoT" in s[0] for s in self.sends))


class TestText(unittest.TestCase):
    def test_empty_text_carries_title_time_and_escapes(self):
        out = rz.empty_text("a<b>", T0)
        self.assertIn("no CoT", out)
        self.assertIn("a&lt;b&gt;", out)
        self.assertIn("turn at", out)
        self.assertNotIn("<pre>", out)


if __name__ == "__main__":
    unittest.main()
