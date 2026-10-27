"""F80 — the blinking emoticon: the activity emoji TOGGLES on every
edit, on both surfaces.

Telegram does not animate emojis; the only honest blink is alternating
the leading emoji per edit (parity of the spin). Damian, 27/10: the
three long phases (pure thinking/spinner, a long edit, a long run) must
be VISIBLY alive in the chat — «quiero verlo visualmente en el bot eso  # noqa: spanish — Damian's request, quoted
con un emoticon que titila». The notice branches (📄 paused, 🖥️ Mac run)  # noqa: spanish — Damian's request, quoted (continuation)
stay static on purpose: they are signs, not life."""
import time
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot import reasoning
from zcode_bot.live_view import LiveTurnView


class TestMacLineBlink(unittest.TestCase):
    """reasoning._live_line_text — the Mac thinking line (ll)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_thinking_toggles_brain_and_thought(self):
        act = {"pending": 0, "tool": "", "steps": 0}
        t0 = time.time() - 10
        even = reasoning._live_line_text("t", t0, None, act, 0)
        odd = reasoning._live_line_text("t", t0, None, act, 1)
        again = reasoning._live_line_text("t", t0, None, act, 2)
        self.assertTrue(even.startswith("🧠"))
        self.assertTrue(odd.startswith("💭"))
        self.assertTrue(again.startswith("🧠"))   # the blink cycles
        # the rest of the line keeps its shape (clock + thinking) — the
        # braille frame advances with the spin by design (rotation)
        for t in (even, odd, again):
            self.assertIn("· ", t)
            self.assertIn("thinking…", t)

    def test_tool_toggles_wrench_and_bolt(self):
        act = {"tool": "Bash", "running": True, "steps": 1,
               "tool_since": time.time() - 5}
        even = reasoning._live_line_text("t", time.time() - 10, None, act, 0)
        odd = reasoning._live_line_text("t", time.time() - 10, None, act, 1)
        self.assertTrue(even.startswith("🔧"))
        self.assertTrue(odd.startswith("⚡"))
        self.assertIn("Running Bash", even)
        self.assertIn("Running Bash", odd)

    def test_writing_toggles_pen_and_memo(self):
        lv = {"reasoningChars": 0, "textChars": 90, "textTail": "x"}
        act = {"pending": 0, "tool": "", "steps": 0}
        even = reasoning._live_line_text("t", time.time() - 10, lv, act, 0)
        odd = reasoning._live_line_text("t", time.time() - 10, lv, act, 1)
        self.assertIn("✍️ writing…", even)
        self.assertIn("📝 writing…", odd)


class TestHeadlessBoxBlink(unittest.TestCase):
    """LiveTurnView._live_box_body — the headless live box."""

    def setUp(self):
        self.zt = support.fresh(self)

    def _bare(self):
        v = object.__new__(LiveTurnView)
        v.cfg = {}
        v.title = "t"
        v.text = ""
        v.events_off = False
        v.reason = ""
        v.n_deltas = 1
        v.spin = 0
        return v

    def test_thinking_toggles(self):
        v = self._bare()
        even = v._live_box_body(10, 0)
        odd = v._live_box_body(10, 1)
        self.assertTrue(even.startswith("🧠"))
        self.assertTrue(odd.startswith("💭"))
        self.assertIn("thinking…", even)

    def test_writing_toggles_gear_and_memo(self):
        v = self._bare()
        v.text = "the reply being written"
        even = v._live_box_body(10, 0)
        odd = v._live_box_body(10, 1)
        self.assertTrue(even.startswith("⚙️"))
        self.assertTrue(odd.startswith("📝"))
        self.assertIn("writing…", even)

    def test_notice_branches_do_not_blink(self):
        v = self._bare()
        v.events_off = True
        a = v._live_box_body(10, 0)
        b = v._live_box_body(10, 1)
        self.assertEqual(a, b)   # a sign, not life
        v2 = self._bare()
        v2.n_deltas = 0
        c = v2._live_box_body(30, 0)
        d = v2._live_box_body(30, 1)
        self.assertEqual(c, d)
        self.assertIn("🖥️", c)

    def test_spin_defaults_to_the_instance_counter(self):
        v = self._bare()
        v.spin = 1
        self.assertTrue(v._live_box_body(10).startswith("💭"))
        v.spin = 2
        self.assertTrue(v._live_box_body(10).startswith("🧠"))


if __name__ == "__main__":
    unittest.main()
