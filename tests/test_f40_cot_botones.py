"""F40/F43/F46 — capture at close with SIZE-based BOXES (fallback of the live watcher).

Damian's model (F43): boxes sealed upon reaching the cap
(COT_BOX_CAP), one button per box. F46: labels «CoT R.C» — reasoning R,
box C within it (never two «CoT 1»). capture() is the fallback
for when the live watcher failed to start.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock  # noqa: F401

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import reasoning as rz  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot.commands import handlers_info as hi  # noqa: E402
from zcode_bot.updates import _cb_text  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
T0 = 1760000000
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"


def _msg(role, *parts):
    return {"info": {"role": role}, "parts": list(parts)}


def _r(text):
    return {"type": "reasoning", "text": text}


HISTORY = [
    _msg("user", {"type": "text", "text": "first"}),
    _msg("assistant", _r("old reasoning A"), {"type": "tool", "tool": "Read"}),
    _msg("assistant", _r("old reasoning B"), {"type": "text", "text": "ok"}),
    _msg("user", {"type": "text", "text": "second"}),
    _msg("assistant", _r("turn reasoning 1"), {"type": "step-finish"}),
    _msg("assistant", {"type": "tool", "tool": "Edit"}, _r("turn reasoning 2")),
    _msg("assistant", {"type": "reasoning", "text": "   "}),  # empty: dropped
    _msg("assistant", {"type": "text", "text": "final answer"}),
]


class TestTurnBoxes(unittest.TestCase):
    def test_two_passes_labels_pass_box(self):
        """The tool closes the 1st reasoning; each fills ONE box — F46:
        «CoT 1.1» and «CoT 2.1», distinct (never two «CoT 1»)."""
        boxes, labels = rz.turn_boxes(HISTORY)
        self.assertEqual(boxes, ["turn reasoning 1", "turn reasoning 2"])
        self.assertEqual(labels, ["CoT 1.1", "CoT 2.1"])

    def test_long_pass_fills_boxes_to_the_cap_plus_tail(self):
        msgs = [_msg("user", {"type": "text", "text": "p"}),
                _msg("assistant", _r("z" * (rz.COT_BOX_CAP * 2 + 500)),
                     {"type": "text", "text": "end"})]
        boxes, labels = rz.turn_boxes(msgs)
        self.assertEqual([len(c) for c in boxes],
                         [rz.COT_BOX_CAP, rz.COT_BOX_CAP, 500])
        self.assertEqual(labels, ["CoT 1.1", "CoT 1.2", "CoT 1.3"])

    def test_contiguous_reasonings_are_the_same_pass(self):
        msgs = [_msg("user", {"type": "text", "text": "p"}),
                _msg("assistant", _r("a"), _r("b")),
                _msg("assistant", _r("c"), {"type": "text", "text": "x"})]
        boxes, labels = rz.turn_boxes(msgs)
        self.assertEqual(boxes, ["abc"])
        self.assertEqual(labels, ["CoT 1.1"])

    def test_without_reasoning(self):
        boxes, labels = rz.turn_boxes([HISTORY[0], HISTORY[-1]])
        self.assertEqual((boxes, labels), ([], []))


class TestSealer(unittest.TestCase):
    """The heart of F43: seals at the cap, close() seals the tail, restarts."""

    def test_seals_as_it_fills(self):
        s = rz.CotSealer(cap=100)
        self.assertEqual(s.feed("x" * 60), [])          # 60: nothing
        filled = s.feed("y" * 60)                        # 120 → box 1
        self.assertEqual([(c, n) for c, n in filled],
                         [("x" * 60 + "y" * 40, 1)])
        # 320 total: the same delta fills boxes 2 and 3 TOGETHER
        filled = s.feed("z" * 200)
        self.assertEqual([n for _c, n in filled], [2, 3])
        self.assertEqual(s.feed("w" * 10), [])  # 330 < 400: nothing

    def test_close_seals_the_tail_and_restarts(self):
        s = rz.CotSealer(cap=100)
        s.feed("a" * 150)  # box 1 sealed, tail of 50
        self.assertEqual(s.close(), [("a" * 50, 2)])          # tail = box 2
        self.assertEqual(s.close(), [])                       # already empty
        filled = s.feed("b" * 120)                        # RESTARTED: 1
        self.assertEqual([n for _c, n in filled], [1])

    def test_in_progress(self):
        s = rz.CotSealer(cap=100)
        s.feed("a" * 250)  # 2 sealed, 50 in progress → box 3
        self.assertEqual(s.in_progress, (50, 3))


class _FakeProto:
    def __init__(self, msgs, resume_error=None):
        self.msgs, self.resume_error, self.seen, self.closed = msgs, resume_error, [], False

    def request(self, method, params=None, timeout=None):
        self.seen.append(method)
        if method == "session/resume":
            return ({"error": {"code": -1, "message": self.resume_error}}
                    if self.resume_error else {"result": {}})
        if method == "session/messages":
            return {"result": {"messages": self.msgs}}
        return {"result": {}}

    def close(self):
        self.closed = True


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends = []
        support.patch(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), True)[1])

    def _make(self, fake):
        support.patch(self, rz.zc_proto, "make", lambda cwd=None, **kw: fake)

    def test_caches_and_sends_one_button_per_box(self):
        fake = _FakeProto(HISTORY)
        self._make(fake)
        n = rz.capture(CFG, SID, "cv", T0, "/w")
        self.assertEqual(n, 2)
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["cajas"], ["turn reasoning 1", "turn reasoning 2"])
        self.assertEqual(d["rotulos"], ["CoT 1.1", "CoT 2.1"])
        text, markup = self.sends[0]
        self.assertIn("2 boxes", text)
        buttons = [b for f in markup["inline_keyboard"] for b in f]
        # two reasonings → «CoT 1.1» and «CoT 2.1»: distinct, global index
        self.assertEqual([b["text"] for b in buttons], ["CoT 1.1", "CoT 2.1"])
        self.assertEqual(buttons[1]["callback_data"], "ct:%s:%d:2" % (SID, T0))
        for b in buttons:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_without_reasoning_announces_in_one_line(self):
        """F45: the turn closed without reasoning → 0 boxes and ONE «no CoT» line
        (no keyboard, no cache) — before, silence was indistinguishable
        from a bug. With the knob off, the usual silence."""
        self._make(_FakeProto([HISTORY[0], HISTORY[-1]]))
        self.assertEqual(rz.capture(dict(CFG, cot_empty_notice=True), SID,
                                     "cv", T0, "/w"), 0)
        self.assertEqual(len(self.sends), 1)
        text, markup = self.sends[0]
        self.assertIn("no CoT", text)
        self.assertIn("«cv»", text)
        self.assertIsNone(markup)
        self.assertFalse(os.path.exists(rz.cache_path(SID, T0)))
        self.sends.clear()
        self._make(_FakeProto([HISTORY[0], HISTORY[-1]]))
        self.assertEqual(rz.capture(CFG, SID, "cv", T0, "/w"), 0)
        self.assertEqual(self.sends, [])

    def test_without_history_sends_nothing(self):
        self._make(_FakeProto([]))
        self.assertIsNone(rz.capture(dict(CFG, cot_empty_notice=True), SID,
                                      "cv", T0, "/w"))
        self.assertEqual(self.sends, [])

    def test_failed_resume_does_not_break(self):
        self._make(_FakeProto(HISTORY, resume_error="no such session"))
        self.assertIsNone(rz.capture(CFG, SID, "cv", T0, "/w"))
        self.assertEqual(self.sends, [])

    def test_more_boxes_than_buttons_caps_the_keyboard(self):
        giant = "l" * (rz.COT_BOX_CAP * 22)  # one reasoning → 22 boxes
        msgs = [HISTORY[3],
                _msg("assistant", _r(giant), {"type": "text", "text": "x"})]
        self._make(_FakeProto(msgs))
        self.assertEqual(rz.capture(CFG, SID, "cv", T0, "/w"), 22)
        text, markup = self.sends[0]
        self.assertIn("22 boxes", text)
        self.assertIn("first %d" % rz.COT_BUTTONS_MAX, text)
        flat = [b for f in markup["inline_keyboard"] for b in f]
        self.assertEqual(len(flat), rz.COT_BUTTONS_MAX)
        self.assertEqual([len(f) for f in markup["inline_keyboard"]], [5] * 4)


class TestShow(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _cache(self, boxes, labels):
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                       "cajas": boxes, "rotulos": labels}, f)

    def test_small_box_whole_with_its_label(self):
        self._cache(["think <b>this</b>", "and this"], ["CoT 1.1", "CoT 2.1"])
        out = rz.show(SID, T0, 1)
        self.assertIsInstance(out, str)
        self.assertIn("🧠 <b>CoT 1.1</b> · «cv»", out)
        self.assertIn("<pre>think &lt;b&gt;this&lt;/b&gt;</pre>", out)

    def test_old_box_without_labels_and_big_in_parts(self):
        # compat: old F40 cache (legacy boxes key, no labels) + giant box
        big = "".join("reasoning line %04d\n" % i for i in range(400))
        with open(rz.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                       "bloques": [big]}, f)
        text, markup = rz.show(SID, T0, 1)
        self.assertIn("🧠 <b>CoT 1</b> · «cv»", text)
        buttons = [b for f in markup["inline_keyboard"] for b in f]
        self.assertEqual(buttons[0]["text"], "[1/%d]" % len(buttons))
        self.assertEqual(buttons[1]["callback_data"],
                         "ct:%s:%d:1:2" % (SID, T0))
        t2, _ = rz.show(SID, T0, 1, 2)
        self.assertIn("part 2/%d" % len(buttons), t2)

    def test_out_of_range_and_without_cache(self):
        self._cache(["one"], ["CoT 1"])
        self.assertIn("Box out of range (1 to 1)", rz.show(SID, T0, 5))
        self.assertIn("no longer saved", rz.show(SID, T0 + 1, 1))

    def test_cmd_cotb_parses_the_button(self):
        self._cache(["one", "two"], ["CoT 1.1", "CoT 2.1"])
        text = _cb_text("ct:%s:%d:2" % (SID, T0))
        self.assertEqual(text, "/cotb %s %d 2" % (SID, T0))
        out = hi._cmd_cotb(CFG, text.split(" ", 1)[1])
        self.assertIn("🧠 <b>CoT 2.1</b>", out)  # the label of ITS own reasoning
        self.assertIn("<pre>two</pre>", out)
        self.assertIn("Usage:", hi._cmd_cotb(CFG, "x"))
        self.assertIn("Invalid button", hi._cmd_cotb(CFG, "a b c"))

    def test_cmd_cotb_edits_the_little_box_message(self):
        """F43c (Damian: 'instead of showing the CoT all the way at the
        bottom, show it where the little box is'): with ph in the cache,
        the content is EDITED into the button message with the full
        keyboard — nothing new drops below."""
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["one", "two"],
                        "rotulos": ["CoT 1.1", "CoT 2.1"], "ph": 555}, f)
        edits = []

        def edit(cfg, ph, text, markup=None):
            edits.append((ph, text, markup))
            return True
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit", edit):
            out = hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertIsNone(out)  # nothing new below: it was an edit
        ph, text, kb = edits[0]
        self.assertEqual(ph, 555)
        self.assertIn("<pre>two</pre>", text)
        self.assertEqual([b["text"] for fl in kb["inline_keyboard"]
                          for b in fl], ["CoT 1.1", "CoT 2.1"])

    def test_cmd_cotb_with_phs_edits_its_pass_message(self):
        """F46: with phs (one button-box per reasoning), the tap edits the
        message of ITS own reasoning — not the usual single ph."""
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["one", "two"],
                        "rotulos": ["CoT 1.1", "CoT 2.1"],
                        "phs": [111, 222], "ph": 999}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (edits.append(ph), True)[1]):
            out = hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertIsNone(out)
        self.assertEqual(edits, [222])  # the 2nd reasoning, not 999

    def test_cmd_cotb_keyboard_of_that_messages_boxes(self):
        """F49 (Damian: «I tapped thinking 1.1 and buttons from 2.1
        suddenly appeared» / «they all piled up in the last box»): the
        viewer edits with the buttons of the boxes that LIVE in that
        message — its own reasoning, the last one too (there is no longer
        a «hub» with the full index; that remains only for the old
        single-message cache, test_cmd_cotb_edits_the_little_box_message)."""
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["one", "two", "three"],
                        "rotulos": ["CoT 1.1", "CoT 1.2", "CoT 2.1"],
                        "phs": [111, 111, 222], "ph": 222}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (edits.append((ph, markup)), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            hi._cmd_cotb(CFG, "%s %d 3" % (SID, T0))

        def texts(kb):
            return [b["text"] for fl in kb["inline_keyboard"] for b in fl]
        self.assertEqual(edits[0][0], 111)
        self.assertEqual(texts(edits[0][1]), ["CoT 1.1", "CoT 1.2"])
        self.assertEqual(edits[1][0], 222)
        self.assertEqual(texts(edits[1][1]), ["CoT 2.1"])  # the last one, same
        # the button callback is still the GLOBAL index
        cb = [b["callback_data"] for fl in edits[0][1]["inline_keyboard"]
              for b in fl]
        self.assertEqual(cb, ["ct:%s:%d:1" % (SID, T0),
                              "ct:%s:%d:2" % (SID, T0)])

    def test_cmd_cotb_late_boxes_share_one_message(self):
        # F49: two reasonings without their own message live in the same
        # late-comers message → the viewer shows BOTH
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["one", "two", "three"],
                        "rotulos": ["CoT 1.1", "CoT 2.1", "CoT 3.1"],
                        "phs": [111, 333, 333], "ph": 111}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (edits.append((ph, markup)), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertEqual(edits[0][0], 333)
        self.assertEqual([b["text"] for fl in edits[0][1]["inline_keyboard"]
                          for b in fl], ["CoT 2.1", "CoT 3.1"])

    def test_cmd_cotb_parts_on_top_and_boxes_below(self):
        # F48b: a big box is navigated in parts WITHOUT losing the boxes
        # of its reasoning (before, parts replaced the whole keyboard)
        import json as _json
        big = "".join("line %04d\n" % i for i in range(400))
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": [big, "two", "three"],
                        "rotulos": ["CoT 1.1", "CoT 1.2", "CoT 2.1"],
                        "phs": [111, 111, 222], "ph": 222}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (edits.append(markup), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
        rows = edits[0]["inline_keyboard"]
        self.assertTrue(rows[0][0]["text"].startswith("[1/"))  # parts
        self.assertEqual([b["text"] for b in rows[-1]],
                         ["CoT 1.1", "CoT 1.2"])              # its boxes

    def test_cmd_cotb_without_ph_or_failed_edit_falls_below_as_before(self):
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                                        lambda *a, **k: False):
            self._cache(["one"], ["CoT 1"])
            out = hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            self.assertIn("<pre>one</pre>", out)


class TestOnStopSpawn(unittest.TestCase):
    """on_stop spawns --cot-botones only if the live watcher didn't take over."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        self.sends = []
        support.patch(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        from zcode_bot.utils import http as zh
        support.patch(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None:
                            self.sends.append(text) or True)
        self.zt.set_active_bot_session(
            {"sid": SID, "title": "cv", "ws": "/w"})
        with open(self.zt.marker_path(SID), "w") as f:
            f.write("{}")

    def _cfg(self, **extra):
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1, notify_stop=True)
        cfg.update(extra)
        return cfg

    def test_spawns_with_the_knob(self):
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        cot = [a for a in self.spawns if "--cot-botones" in a]
        self.assertEqual(len(cot), 1)
        i = cot[0].index("--cot-botones")
        self.assertEqual(cot[0][i + 1], SID)
        self.assertLess(abs(int(cot[0][i + 3]) - time.time()), 60)

    def test_knob_off_does_not_spawn(self):
        self.zt.on_stop(self._cfg(mac_cot_buttons=False),
                        {"sessionId": SID, "responseText": "done"})
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_with_finalized_cache_does_not_spawn(self):
        # the SAME t0 on_stop will read: the live watcher closed its cache
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.save_cache(SID, t0m, "cv", [], [], finalizado=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_live_cache_with_watcher_does_not_spawn(self):
        # F51a: the live watcher is IN FLIGHT (live pid, maybe resuming
        # with the heartbeat expired) — the cache hasn't closed but its owner exists
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.pid_write(SID)
        rz.save_cache(SID, t0m, "cv", [], [], vivo=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_live_cache_without_watcher_spawns(self):
        # F51a (the real case): the watcher DIED midway and its cache stayed
        # vivo=True — before, that cache silenced the fallback and the
        # turn closed with NO boxes. Now the fallback runs anyway.
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.save_cache(SID, t0m, "cv", ["x"], ["CoT 1.1"], vivo=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        cot = [a for a in self.spawns if "--cot-botones" in a]
        self.assertEqual(len(cot), 1)

    def test_the_check_announces_the_slow_close(self):
        # F51a: without the live watcher's cache the fallback takes ~2 min —
        # the ✅ says so, so the delayed boxes aren't read as another turn's
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        self.assertTrue(any("reasoning boxes on the way" in t
                            and "Turn finished" in t for t in self.sends))

    def test_with_cache_the_check_announces_no_delay(self):
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.save_cache(SID, t0m, "cv", [], [], finalizado=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        self.assertFalse(any("on the way" in t for t in self.sends))


if __name__ == "__main__":
    unittest.main()
