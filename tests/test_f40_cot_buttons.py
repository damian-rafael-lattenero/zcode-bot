"""F40/F43/F46 — capture at close with SIZE-based BOXES (fallback of the live watcher).

Damian's model (F43): boxes sealed upon reaching the cap
(COT_BOX_CAP), one button per box. F46: labels «CoT R.C» — reasoning R,
box C within it (never two «CoT 1»). capture() is the fallback
for when the live watcher failed to start.
"""
import json
import os
import time
import unittest
from unittest import mock  # noqa: F401

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
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
        boxes, labels = reasoning.turn_boxes(HISTORY)
        self.assertEqual(boxes, ["turn reasoning 1", "turn reasoning 2"])
        self.assertEqual(labels, ["CoT 1.1", "CoT 2.1"])

    def test_long_pass_fills_boxes_to_the_cap_plus_tail(self):
        msgs = [_msg("user", {"type": "text", "text": "p"}),
                _msg("assistant", _r("z" * (reasoning.COT_BOX_CAP * 2 + 500)),
                     {"type": "text", "text": "end"})]
        boxes, labels = reasoning.turn_boxes(msgs)
        self.assertEqual([len(c) for c in boxes],
                         [reasoning.COT_BOX_CAP, reasoning.COT_BOX_CAP, 500])
        self.assertEqual(labels, ["CoT 1.1", "CoT 1.2", "CoT 1.3"])

    def test_contiguous_reasonings_are_the_same_pass(self):
        msgs = [_msg("user", {"type": "text", "text": "p"}),
                _msg("assistant", _r("a"), _r("b")),
                _msg("assistant", _r("c"), {"type": "text", "text": "x"})]
        boxes, labels = reasoning.turn_boxes(msgs)
        self.assertEqual(boxes, ["abc"])
        self.assertEqual(labels, ["CoT 1.1"])

    def test_without_reasoning(self):
        boxes, labels = reasoning.turn_boxes([HISTORY[0], HISTORY[-1]])
        self.assertEqual((boxes, labels), ([], []))


class TestSealer(unittest.TestCase):
    """The heart of F43: seals at the cap, close() seals the tail, restarts."""

    def test_seals_as_it_fills(self):
        s = reasoning.CotSealer(cap=100)
        self.assertEqual(s.feed("x" * 60), [])          # 60: nothing
        filled = s.feed("y" * 60)                        # 120 → box 1
        self.assertEqual([(c, n) for c, n in filled],
                         [("x" * 60 + "y" * 40, 1)])
        # 320 total: the same delta fills boxes 2 and 3 TOGETHER
        filled = s.feed("z" * 200)
        self.assertEqual([n for _c, n in filled], [2, 3])
        self.assertEqual(s.feed("w" * 10), [])  # 330 < 400: nothing

    def test_close_seals_the_tail_and_restarts(self):
        s = reasoning.CotSealer(cap=100)
        s.feed("a" * 150)  # box 1 sealed, tail of 50
        self.assertEqual(s.close(), [("a" * 50, 2)])          # tail = box 2
        self.assertEqual(s.close(), [])                       # already empty
        filled = s.feed("b" * 120)                        # RESTARTED: 1
        self.assertEqual([n for _c, n in filled], [1])

    def test_in_progress(self):
        s = reasoning.CotSealer(cap=100)
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
        support.patch(self, reasoning.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None, a_outbox=True:
                        (self.sends.append((text, markup)), True)[1])

    def _make(self, fake):
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: fake)

    def test_caches_and_sends_one_button_per_box(self):
        fake = _FakeProto(HISTORY)
        self._make(fake)
        n = reasoning.capture(CFG, SID, "cv", T0, "/w")
        self.assertEqual(n, 2)
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["boxes"], ["turn reasoning 1", "turn reasoning 2"])
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 2.1"])
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
        self.assertEqual(reasoning.capture(dict(CFG, cot_empty_notice=True), SID,
                                               "cv", T0, "/w"), 0)
        self.assertEqual(len(self.sends), 1)
        text, markup = self.sends[0]
        self.assertIn("no CoT", text)
        self.assertIn("«cv»", text)
        self.assertIsNone(markup)
        self.assertFalse(os.path.exists(reasoning.cache_path(SID, T0)))
        self.sends.clear()
        self._make(_FakeProto([HISTORY[0], HISTORY[-1]]))
        self.assertEqual(reasoning.capture(CFG, SID, "cv", T0, "/w"), 0)
        self.assertEqual(self.sends, [])

    def test_without_history_sends_nothing(self):
        self._make(_FakeProto([]))
        self.assertIsNone(reasoning.capture(dict(CFG, cot_empty_notice=True), SID,
                                                 "cv", T0, "/w"))
        self.assertEqual(self.sends, [])

    def test_failed_resume_does_not_break(self):
        self._make(_FakeProto(HISTORY, resume_error="no such session"))
        self.assertIsNone(reasoning.capture(CFG, SID, "cv", T0, "/w"))
        self.assertEqual(self.sends, [])

    def test_more_boxes_than_buttons_caps_the_keyboard(self):
        giant = "l" * (reasoning.COT_BOX_CAP * 22)  # one reasoning → 22 boxes
        msgs = [HISTORY[3],
                _msg("assistant", _r(giant), {"type": "text", "text": "x"})]
        self._make(_FakeProto(msgs))
        self.assertEqual(reasoning.capture(CFG, SID, "cv", T0, "/w"), 22)
        text, markup = self.sends[0]
        self.assertIn("22 boxes", text)
        self.assertIn("first %d" % reasoning.COT_BUTTONS_MAX, text)
        flat = [b for f in markup["inline_keyboard"] for b in f]
        self.assertEqual(len(flat), reasoning.COT_BUTTONS_MAX)
        self.assertEqual([len(f) for f in markup["inline_keyboard"]], [5] * 4)


class TestShow(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _cache(self, boxes, labels):
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)), exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "title": "cv", "t0": T0,
                       "boxes": boxes, "labels": labels}, f)

    def test_small_box_whole_with_its_label(self):
        self._cache(["think <b>this</b>", "and this"], ["CoT 1.1", "CoT 2.1"])
        out = reasoning.show(SID, T0, 1)
        self.assertIsInstance(out, str)
        self.assertIn("🧠 <b>CoT 1.1</b> · «cv»", out)
        self.assertIn("<pre>think &lt;b&gt;this&lt;/b&gt;</pre>", out)

    def test_old_box_without_labels_and_big_in_parts(self):
        # compat: old F40 cache (legacy boxes key, no labels) + giant box
        big = "".join("reasoning line %04d\n" % i for i in range(400))
        with open(reasoning.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "titulo": "cv", "t0": T0,  # noqa: spanish — legacy cache shape fixture
                       "bloques": [big]}, f)
        text, markup = reasoning.show(SID, T0, 1)
        self.assertIn("🧠 <b>CoT 1</b> · «cv»", text)
        buttons = [b for f in markup["inline_keyboard"] for b in f]
        self.assertEqual(buttons[0]["text"], "[1/%d]" % len(buttons))
        self.assertEqual(buttons[1]["callback_data"],
                         "ct:%s:%d:1:2" % (SID, T0))
        t2, _ = reasoning.show(SID, T0, 1, 2)
        self.assertIn("part 2/%d" % len(buttons), t2)

    def test_out_of_range_and_without_cache(self):
        self._cache(["one"], ["CoT 1"])
        self.assertIn("Box out of range (1 to 1)", reasoning.show(SID, T0, 5))
        self.assertIn("no longer saved", reasoning.show(SID, T0 + 1, 1))

    def test_cmd_cotb_parses_the_button(self):
        self._cache(["one", "two"], ["CoT 1.1", "CoT 2.1"])
        text = _cb_text("ct:%s:%d:2" % (SID, T0))
        self.assertEqual(text, "/cotb %s %d 2" % (SID, T0))
        out = hi._cmd_cotb(CFG, text.split(" ", 1)[1])
        self.assertIn("🧠 <b>CoT 2.1</b>", out)  # the label of ITS own reasoning
        self.assertIn("<pre>two</pre>", out)
        self.assertIn("Usage:", hi._cmd_cotb(CFG, "x"))
        self.assertIn("Invalid button", hi._cmd_cotb(CFG, "a b c"))

    def test_cmd_cotb_opens_the_reader_window(self):
        """F56: the tap opens the READER (📖, one per chat) — the round's
        live message is never the tap target. Born via send on the first
        tap; reused by edit from then on."""
        import json as _json
        hi._READER["ph"] = None
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)), exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "title": "cv", "t0": T0,
                        "boxes": ["one", "two"],
                        "labels": ["CoT 1.1", "CoT 2.1"], "ph": 555}, f)
        sends, edits = [], []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               sends.append((text, markup))
                               or 9001), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (edits.append(ph), True)[1]):
            out = hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertIsNone(out)          # the reader took it
        self.assertEqual(edits, [])     # nothing edited: the reader is NEW
        text, kb = sends[0]
        self.assertTrue(text.startswith("📖"))          # marked as reader
        self.assertIn("<pre>two</pre>", text)
        self.assertEqual([b["text"] for fl in kb["inline_keyboard"]
                          for b in fl], ["CoT 1.1", "CoT 2.1", "✕"])
        self.assertEqual(hi._READER["ph"], 9001)
        # second tap: the SAME reader is edited, nothing new below
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               sends.append((text, markup)) or None), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (edits.append(ph), True)[1]):
            out = hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
        self.assertIsNone(out)
        self.assertEqual(edits, [9001])
        self.assertEqual(len(sends), 1)

    def test_cmd_cotb_never_edits_the_rounds_live_message(self):
        """THE regression (Damian, 22/10): reading 1.3 while 1.4 is born.
        The producer keeps editing the round's message (111/222) — the
        tap NEVER does: its edits land on the reader only."""
        import json as _json
        hi._READER["ph"] = None
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)), exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "title": "cv", "t0": T0,
                        "boxes": ["one", "two"],
                        "labels": ["CoT 1.1", "CoT 2.1"],
                        "phs": [111, 222], "ph": 999}, f)
        touched = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (touched.append(ph), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
            self.assertEqual(touched, [])   # first tap: the reader is BORN
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))  # now it edits
            self.assertEqual(touched, [9001])  # the reader, and ONLY it
            self.assertNotIn(111, touched)
            self.assertNotIn(222, touched)
            self.assertNotIn(999, touched)

    def test_cmd_cotb_keyboard_of_that_rounds_boxes(self):
        """F49 (Damian: «I tapped thinking 1.1 and buttons from 2.1
        suddenly appeared»): the reader carries the buttons of the boxes
        that LIVE in the same message the box belongs to — its own
        reasoning, the last one too (the full index only for the old
        single-message caches)."""
        import json as _json
        hi._READER["ph"] = None
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)), exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "title": "cv", "t0": T0,
                        "boxes": ["one", "two", "three"],
                        "labels": ["CoT 1.1", "CoT 1.2", "CoT 2.1"],
                        "phs": [111, 111, 222], "ph": 222}, f)
        kbs = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (kbs.append(markup), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 3" % (SID, T0))   # primes the reader
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            hi._cmd_cotb(CFG, "%s %d 3" % (SID, T0))

        def texts(kb):
            return [b["text"] for fl in kb["inline_keyboard"] for b in fl]
        self.assertEqual(len(kbs), 2)         # the prime tap SENT, not edited
        self.assertEqual(texts(kbs[0]), ["CoT 1.1", "CoT 1.2", "✕"])
        self.assertEqual(texts(kbs[1]), ["CoT 2.1", "✕"])
        # the button callback is still the GLOBAL index
        cb = [b["callback_data"] for fl in kbs[0]["inline_keyboard"]
              for b in fl]
        self.assertEqual(cb, ["ct:%s:%d:1" % (SID, T0),
                              "ct:%s:%d:2" % (SID, T0), "cc"])

    def test_cmd_cotb_late_boxes_share_one_reader_section(self):
        # F49: two reasonings without their own message live in the same
        # late-comers message → the reader shows BOTH (plus ✕)
        import json as _json
        hi._READER["ph"] = None
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)), exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "title": "cv", "t0": T0,
                        "boxes": ["one", "two", "three"],
                        "labels": ["CoT 1.1", "CoT 2.1", "CoT 3.1"],
                        "phs": [111, 333, 333], "ph": 111}, f)
        kbs = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (kbs.append(markup), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))   # primes the reader
            hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertEqual([b["text"] for fl in kbs[0]["inline_keyboard"]
                          for b in fl], ["CoT 2.1", "CoT 3.1", "✕"])

    def test_cmd_cotb_parts_on_top_boxes_and_close_below(self):
        # F48b: a big box is navigated in parts WITHOUT losing the boxes
        # of its reasoning; F56: ✕ closes the reader
        import json as _json
        hi._READER["ph"] = None
        big = "".join("line %04d\n" % i for i in range(400))
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)), exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "title": "cv", "t0": T0,
                        "boxes": [big, "two", "three"],
                        "labels": ["CoT 1.1", "CoT 1.2", "CoT 2.1"],
                        "phs": [111, 111, 222], "ph": 222}, f)
        kbs = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (kbs.append(markup), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))   # primes the reader
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
        rows = kbs[0]["inline_keyboard"]
        self.assertTrue(rows[0][0]["text"].startswith("[1/"))  # parts
        self.assertEqual([b["text"] for b in rows[-2]],
                         ["CoT 1.1", "CoT 1.2"])              # its boxes
        self.assertEqual([b["text"] for b in rows[-1]], ["✕"])  # F56

    def test_cmd_cotb_failed_send_falls_below_as_before(self):
        from zcode_bot.utils import http as zh
        hi._READER["ph"] = None
        with mock.patch.object(zh, "send", lambda *a, **k: None):
            self._cache(["one"], ["CoT 1"])
            out = hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            self.assertIn("<pre>one</pre>", out)


class TestCotReader(unittest.TestCase):
    """F56 — the reader window's lifecycle: born on tap, reused, closed
    by ✕ (cc → /cotx deletes the message), and its id lives only in
    poller memory."""

    def setUp(self):
        import json as _json
        hi._READER["ph"] = None
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)),
                    exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "title": "cv", "t0": T0,
                        "boxes": ["one", "two"],
                        "labels": ["CoT 1.1", "CoT 2.1"], "ph": 555}, f)

    def test_close_button_deletes_the_reader(self):
        deleted = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(zh, "_delete",
                               lambda cfg, ph: deleted.append(ph) or True):
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            self.assertEqual(hi._READER["ph"], 9001)
            out = hi._cmd_cotx(CFG, "")
            self.assertIsNone(out)
            self.assertEqual(deleted, [9001])
            self.assertIsNone(hi._READER["ph"])

    def test_after_close_the_next_tap_opens_a_new_reader(self):
        from zcode_bot.utils import http as zh
        sends, deleted = [], []
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               sends.append(1)
                               or 9001), \
             mock.patch.object(zh, "_delete",
                               lambda cfg, ph: deleted.append(ph) or True), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None: False):
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            hi._cmd_cotx(CFG, "")
            hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertEqual(len(sends), 2)   # a fresh reader was born

    def test_edit_failure_rebirths_the_reader(self):
        # a stale reader id (message deleted by hand, say): the edit
        # fails → a new reader is sent in its place
        from zcode_bot.utils import http as zh
        hi._READER["ph"] = 4242           # stale
        sends = []
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               sends.append(1)
                               or 9002), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None: False):
            out = hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
        self.assertIsNone(out)
        self.assertEqual(hi._READER["ph"], 9002)


class TestF59InPlaceTap(unittest.TestCase):
    """F59 — the tap on a box's OWN frozen message expands it THERE
    (Damian, 22/10: deploy it INSIDE its own message): nothing is born
    at the bottom of the chat, ⤴️ folds the message back to its exact
    frozen line. Every other tap surface keeps the F56 reader."""

    def setUp(self):
        self.zt = support.fresh(self)
        hi._READER["ph"] = None

    def _cache(self, boxes, labels, phs, perbox=True):
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)),
                    exist_ok=True)
        d = {"sid": SID, "title": "cv", "t0": T0,
             "boxes": boxes, "labels": labels, "phs": phs}
        if perbox:
            d["perbox"] = True
        with open(reasoning.cache_path(SID, T0), "w") as f:
            json.dump(d, f)

    def test_tap_expands_the_boxs_own_message_in_place(self):
        self._cache(["one", "two"], ["CoT 1.1", "CoT 2.1"], [111, 222])
        from zcode_bot.utils import http as zh
        sends, edits = [], []
        with mock.patch.object(
                zh, "send",
                lambda cfg, text, loud=None, markup=None, a_outbox=True:
                    (sends.append(text), 9001)[1]), \
             mock.patch.object(
                zh, "_live_edit",
                lambda cfg, ph, text, markup=None:
                    (edits.append((ph, text, markup)), True)[1]):
            out = hi._cmd_cotb(CFG, "%s %d 2 ph=222" % (SID, T0))
        self.assertIsNone(out)
        self.assertEqual(sends, [])       # NOTHING born at the bottom
        self.assertEqual(len(edits), 1)
        ph, text, markup = edits[0]
        self.assertEqual(ph, 222)         # ITS OWN message, not a reader
        self.assertEqual(text, "🧠 <b>CoT 2.1</b> · «cv»\n<pre>two</pre>")
        self.assertEqual(markup, {"inline_keyboard": [[
            {"text": "⤴️ Collapse",
             "callback_data": "cr:%s:%d:2" % (SID, T0)}]]})
        self.assertIsNone(hi._READER["ph"])   # the reader never opened

    def test_parts_row_stays_above_the_collapse_row(self):
        # a big box navigates in parts INSIDE its own message too
        big = "".join("reasoning line %04d\n" % i for i in range(400))
        self._cache([big, "two"], ["CoT 1.1", "CoT 2.1"], [111, 222])
        from zcode_bot.utils import http as zh
        edits = []
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(
                zh, "_live_edit",
                lambda cfg, ph, text, markup=None:
                    (edits.append((ph, text, markup)), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 1 ph=111" % (SID, T0))
            hi._cmd_cotb(CFG, "%s %d 1 2 ph=111" % (SID, T0))  # part 2
        self.assertEqual([e[0] for e in edits], [111, 111])
        rows = edits[0][2]["inline_keyboard"]
        self.assertTrue(rows[0][0]["text"].startswith("[1/"))   # parts
        self.assertEqual(rows[-1][0]["text"], "⤴️ Collapse")     # below
        self.assertIn("part 2/", edits[1][1])                    # in place

    def test_collapse_restores_the_frozen_line_exactly(self):
        self._cache(["one", "two"], ["CoT 1.1", "CoT 2.1"], [111, 222])
        from zcode_bot.utils import http as zh
        edits = []
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(
                zh, "_live_edit",
                lambda cfg, ph, text, markup=None:
                    (edits.append((ph, text, markup)), True)[1]):
            out = hi._cmd_cotr(CFG, "%s %d 2 ph=222" % (SID, T0))
        self.assertIsNone(out)
        self.assertEqual(len(edits), 1)
        ph, text, markup = edits[0]
        self.assertEqual(ph, 222)
        self.assertEqual(text,
                         reasoning.box_done_text("CoT 2.1", len("two")))
        self.assertEqual(markup, {"inline_keyboard": reasoning.box_buttons(
            SID, T0, ["CoT 2.1"], start=2)})

    def test_a_tap_on_another_surface_opens_the_reader(self):
        # the reader's OWN sibling button (ph 999 ≠ the box's ph): F56
        self._cache(["one", "two"], ["CoT 1.1", "CoT 2.1"], [111, 222])
        from zcode_bot.utils import http as zh
        sends, edits = [], []
        with mock.patch.object(
                zh, "send",
                lambda cfg, text, loud=None, markup=None, a_outbox=True:
                    (sends.append(text), 9001)[1]), \
             mock.patch.object(
                zh, "_live_edit",
                lambda cfg, ph, text, markup=None:
                    (edits.append(ph), True)[1]):
            out = hi._cmd_cotb(CFG, "%s %d 2 ph=999" % (SID, T0))
        self.assertIsNone(out)
        self.assertEqual(edits, [])       # the reader is BORN, not edited
        self.assertTrue(sends[0].startswith("📖"))
        self.assertEqual(hi._READER["ph"], 9001)

    def test_legacy_round_cache_keeps_the_reader(self):
        # pre-F57 phs (several boxes share a round message): NEVER an
        # in-place expansion — the restore would rebuild a round
        # message as a single-box one
        self._cache(["one", "two"], ["CoT 1.1", "CoT 1.2"], [111, 111],
                    perbox=False)
        from zcode_bot.utils import http as zh
        sends, edits = [], []
        with mock.patch.object(
                zh, "send",
                lambda cfg, text, loud=None, markup=None, a_outbox=True:
                    (sends.append(text), 9001)[1]), \
             mock.patch.object(
                zh, "_live_edit",
                lambda cfg, ph, text, markup=None:
                    (edits.append(ph), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 2 ph=111" % (SID, T0))
        self.assertEqual(edits, [])
        self.assertTrue(sends[0].startswith("📖"))

    def test_throttled_edit_falls_back_to_the_reader(self):
        self._cache(["one", "two"], ["CoT 1.1", "CoT 2.1"], [111, 222])
        from zcode_bot.utils import http as zh
        sends = []
        with mock.patch.object(
                zh, "send",
                lambda cfg, text, loud=None, markup=None, a_outbox=True:
                    (sends.append(text), 9001)[1]), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None: False):
            out = hi._cmd_cotb(CFG, "%s %d 2 ph=222" % (SID, T0))
        self.assertIsNone(out)
        self.assertTrue(sends[0].startswith("📖"))   # degraded, not mute
        self.assertEqual(hi._READER["ph"], 9001)

    def test_collapse_guards(self):
        self._cache(["one", "two"], ["CoT 1.1", "CoT 2.1"], [111, 222])
        from zcode_bot.utils import http as zh
        touched = []
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(
                zh, "_live_edit",
                lambda cfg, ph, text, markup=None:
                    (touched.append(ph), True)[1]):
            out = hi._cmd_cotr(CFG, "%s %d 2 ph=111" % (SID, T0))  # not its
            self.assertIsNone(out)
            out = hi._cmd_cotr(CFG, "%s %d 2" % (SID, T0))         # no ph
            self.assertIn("Usage:", out)
            out = hi._cmd_cotr(CFG, "%s %d 2 ph=222" % (SID, T0 + 7))
            self.assertIsNone(out)                       # no cache there
        self.assertEqual(touched, [])   # nothing restored incorrectly


class TestLegacyCacheCompat(unittest.TestCase):
    """P6 — a cache written by a pre-deploy build (Spanish keys
    titulo/cajas/rotulos/vivo/finalizado) keeps feeding its buttons:  # noqa: spanish — the old cache shape, documented compat test
    show reads it, /cotb builds the keyboard from its labels and
    close_cache clears the old live flag. A 24h-old Telegram button
    must never break."""

    def setUp(self):
        self.zt = support.fresh(self)

    def _old_cache(self, **extra):
        os.makedirs(os.path.dirname(reasoning.cache_path(SID, T0)), exist_ok=True)
        with open(reasoning.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "titulo": "cv", "t0": T0,  # noqa: spanish — legacy cache shape fixture
                       "cajas": ["old one", "old two"],  # noqa: spanish — legacy cache shape fixture
                       "rotulos": ["CoT 1.1", "CoT 1.2"], **extra}, f)  # noqa: spanish — legacy cache shape fixture

    def test_old_cache_still_shows(self):
        self._old_cache(finalizado=True)
        out = reasoning.show(SID, T0, 2)
        self.assertIn("🧠 <b>CoT 1.2</b> · «cv»", out)
        self.assertIn("<pre>old two</pre>", out)

    def test_old_cache_still_feeds_ct_buttons(self):
        # the tap (ct:sid:t0:n → /cotb) opens the reader using the OLD
        # labels; the old ph is simply not the target anymore (F56)
        hi._READER["ph"] = 9001    # the reader already exists
        self._old_cache(ph=777)
        kbs = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                               9001), \
             mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, text, markup=None:
                               (kbs.append(markup), True)[1]):
            out = hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
        self.assertIsNone(out)  # the reader took it: nothing new below
        self.assertEqual([b["text"] for row in kbs[0]["inline_keyboard"]
                          for b in row], ["CoT 1.1", "CoT 1.2", "✕"])

    def test_old_cache_still_closes(self):
        self._old_cache(vivo=True)
        self.assertTrue(reasoning.close_cache(SID, T0, error="watcher died"))
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertTrue(d["done"])
        self.assertNotIn("vivo", d)
        # what the taps read is preserved untouched
        self.assertEqual(d["cajas"], ["old one", "old two"])  # noqa: spanish — asserts the legacy shape is preserved
        self.assertEqual(d["rotulos"], ["CoT 1.1", "CoT 1.2"])  # noqa: spanish — asserts the legacy shape is preserved
        self.assertEqual(d["titulo"], "cv")  # noqa: spanish — asserts the legacy shape is preserved

    def test_closed_or_live_reads_the_old_flags(self):
        self._old_cache(finalizado=True)
        self.assertTrue(reasoning.cache_closed_or_live(SID, T0))
        self._old_cache(vivo=True)
        # live old cache with no watcher PROCESS → False: the fallback runs
        self.assertFalse(reasoning.cache_closed_or_live(SID, T0))


class TestOnStopSpawn(unittest.TestCase):
    """on_stop spawns --cot-buttons only if the live watcher didn't take over."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        self.sends = []
        support.patch(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        from zcode_bot.utils import http as zh
        support.patch(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None, a_outbox=True:
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
        cot = [a for a in self.spawns if "--cot-buttons" in a]
        self.assertEqual(len(cot), 1)
        i = cot[0].index("--cot-buttons")
        self.assertEqual(cot[0][i + 1], SID)
        self.assertLess(abs(int(cot[0][i + 3]) - time.time()), 60)

    def test_knob_off_does_not_spawn(self):
        self.zt.on_stop(self._cfg(mac_cot_buttons=False),
                        {"sessionId": SID, "responseText": "done"})
        self.assertEqual([a for a in self.spawns if "--cot-buttons" in a], [])

    def test_with_finalized_cache_does_not_spawn(self):
        # the SAME t0 on_stop will read: the live watcher closed its cache
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        reasoning.save_cache(SID, t0m, "cv", [], [], done=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        self.assertEqual([a for a in self.spawns if "--cot-buttons" in a], [])

    def test_live_cache_with_watcher_does_not_spawn(self):
        # F51a: the live watcher is IN FLIGHT (live pid, maybe resuming
        # with the heartbeat expired) — the cache hasn't closed but its owner exists
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        reasoning.pid_write(SID)
        reasoning.save_cache(SID, t0m, "cv", [], [], live=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        self.assertEqual([a for a in self.spawns if "--cot-buttons" in a], [])

    def test_live_cache_without_watcher_spawns(self):
        # F51a (the real case): the watcher DIED midway and its cache stayed
        # live=True — before, that cache silenced the fallback and the
        # turn closed with NO boxes. Now the fallback runs anyway.
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        reasoning.save_cache(SID, t0m, "cv", ["x"], ["CoT 1.1"], live=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        cot = [a for a in self.spawns if "--cot-buttons" in a]
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
        reasoning.save_cache(SID, t0m, "cv", [], [], done=True)
        self.zt.on_stop(self._cfg(mac_cot_buttons=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})
        self.assertFalse(any("on the way" in t for t in self.sends))


if __name__ == "__main__":
    unittest.main()
