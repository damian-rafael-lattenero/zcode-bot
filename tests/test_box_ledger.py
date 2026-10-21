"""F54d — BoxLedger unit tests: the pure bookkeeping shared by the Mac
watcher and the phone's live view, before either side adopts it."""
import unittest

from zcode_bot.reasoning import (BoxLedger, _parse_reasoned_label)


class TestPhonePath(unittest.TestCase):
    def test_label_is_round_box(self):
        led = BoxLedger()
        led.round = 2
        self.assertEqual(led.label(3), "CoT 2.3")

    def test_add_and_round_span_roundtrip(self):
        led = BoxLedger()
        led.round = 1
        led.add("b1", led.label(1))
        led.add("b2", led.label(2))
        led.next_round()
        led.add("b3", led.label(1))
        self.assertEqual(_parse_reasoned_label("CoT 1.2"), 1)
        self.assertEqual(led.round_span(1), (1, 3))
        self.assertEqual(led.round_span(2), (3, 4))
        self.assertIsNone(led.round_span(3))

    def test_next_round_advances_and_parks_ph(self):
        led = BoxLedger()
        led.round = 1
        led.add("b1", led.label(1))
        led.set_round_ph(601)
        led.add("b2", led.label(2))
        span = led.next_round()
        self.assertEqual(span, (1, 1, 3))
        self.assertEqual(led.round, 2)
        self.assertEqual(led.round_start, 3)
        self.assertIsNone(led.round_ph)
        self.assertEqual(led.round_phs, {1: 601})

    def test_next_round_with_no_boxes_consumes_nothing(self):
        led = BoxLedger()
        led.round = 4
        led.set_round_ph(77)
        self.assertIsNone(led.next_round())     # F48c
        self.assertEqual(led.round, 4)          # number NOT consumed
        self.assertEqual(led.round_start, 1)
        self.assertEqual(led.round_phs, {4: 77})

    def test_set_round_ph_none_keeps_the_parked_one(self):
        led = BoxLedger()
        led.round = 1
        led.set_round_ph(601)
        led.set_round_ph(None)
        self.assertIsNone(led.round_ph)
        self.assertEqual(led.round_phs, {1: 601})


class TestCacheKwargs(unittest.TestCase):
    def test_phs_derived_per_box_of_its_round(self):
        led = BoxLedger()
        led.round = 1
        led.add("b1", led.label(1))
        led.add("b2", led.label(2))
        led.set_round_ph(601)
        led.next_round()
        led.add("b3", led.label(1))
        led.set_round_ph(602)
        self.assertEqual(led.cache_kwargs(),
                         {"boxes": ["b1", "b2", "b3"],
                          "labels": ["CoT 1.1", "CoT 1.2", "CoT 2.1"],
                          "phs": [601, 601, 602]})

    def test_no_round_phs_gives_all_none(self):
        led = BoxLedger()
        led.add("b1", "CoT 1.1")
        self.assertEqual(led.cache_kwargs()["phs"], [None])

    def test_returns_copies(self):
        led = BoxLedger()
        led.add("b1", "CoT 1.1")
        kw = led.cache_kwargs()
        kw["boxes"].append("mutated")
        kw["phs"].append(9)
        self.assertEqual(led.cache_kwargs()["boxes"], ["b1"])


class TestMacPath(unittest.TestCase):
    def test_sync_reports_new_round_starts(self):
        led = BoxLedger()
        starts = led.sync(["b1", "b2", "b3", "b4"],
                          ["CoT 1.1", "CoT 1.2", "CoT 2.1", "CoT 2.2"])
        self.assertEqual(starts, [(0, 1), (2, 2)])
        self.assertEqual(led.boxes, ["b1", "b2", "b3", "b4"])

    def test_sync_second_poll_reports_only_the_new_ones(self):
        led = BoxLedger()
        led.sync(["b1"], ["CoT 1.1"])
        led.advance_to(1, 0)
        starts = led.sync(["b1", "b2", "b3"], ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(starts, [(2, 2)])   # 1.2 is same round, 2.1 is new

    def test_sync_replacement_semantics(self):
        led = BoxLedger()
        led.sync(["b1", "b2"], ["CoT 1.1", "CoT 1.2"])
        led.sync(["only"], ["CoT 1.1"])
        self.assertEqual(led.boxes, ["only"])
        # caller keeps the monotonic guard (live_ones) — documented

    def test_advance_to_parks_and_returns_the_closed_pair(self):
        led = BoxLedger()
        led.sync(["b1", "b2", "b3"], ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        led.advance_to(1, 0)
        led.set_round_ph(601)
        closed = led.advance_to(2, 2)
        self.assertEqual(closed, (1, 601))
        self.assertEqual(led.round_phs, {1: 601})
        self.assertEqual(led.round_start, 3)

    def test_advance_from_anti_silence_adopts_the_ph(self):
        led = BoxLedger()          # round 0: the F49 anti-silence adopt
        led.set_round_ph(600)
        closed = led.advance_to(1, 0)
        self.assertEqual(closed, (None, None))   # nothing closes...
        self.assertEqual(led.round_ph, 600)      # ...the ph is ADOPTED
        self.assertEqual(led.round_phs, {})      # round 0 parks nothing
        self.assertEqual(led.round, 1)


if __name__ == "__main__":
    unittest.main()
