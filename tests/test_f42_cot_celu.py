"""F42/F43 — the CoT of the PHONE's turns in size-based boxes.

Damian's model: "a box has a size; when the CoT reaches the cap, the box
fills up and is sent with its number; it continues and stops again at
the cap, and so buttons keep arriving until the CoT ends; then the chat
writes or sends a form; and when it thinks again, it sends the CoT
again numbered from 1". The worker feeds a CotSealer with the
reasoning_delta: the box travels as a button AS SOON AS IT FILLS (live,
mid-reasoning), not when the phase closes.
"""
import os
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
from zcode_bot.live_view import LiveTurnView  # noqa: E402

CAP = reasoning.COT_BOX_CAP


# R13: the local _FakeC/_Th moved to support — FakeEventsClient records
# .requests as METHOD NAMES (the old tuple shape nobody read here) plus
# .req_params with the (method, params) tuples. _ByStage below (staged
# replay) stays local: it is a different behavior, not a duplicate.


def _ev(eid, kind, delta=""):
    p = {"kind": kind}
    if delta:
        p["delta"] = delta
    return {"eventId": eid, "payload": p}


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1)
    cfg.update(extra)
    return cfg


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def _view(self, c, cfg=None, **dep_extra):
        self.sends = []       # (text, markup)
        self.live_sends = []
        self.edits = []       # (mid, text, markup)
        deps = {
            "send": lambda cfg, text, loud=None, markup=None, a_outbox=True:
                (self.sends.append((text, markup)), 601)[1],
            "live_send": lambda cfg, text:
                (self.live_sends.append(text), 620)[1],
            "live_edit": lambda cfg, mid, text, markup=None:
                (self.edits.append((mid, text, markup)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: None,
        }
        deps.update(dep_extra)
        return LiveTurnView(c, cfg or _cfg(self.zt, cot_buttons=True),
                            self.T1, 1760000000.0, "testing", 777, support.FakeThread(2),
                            last_id=None, deps=deps)

    def _buttons(self, kb):
        return [b["text"] for row in (kb or {}).get("inline_keyboard", [])
                for b in row]


# One reasoning that crosses the cap mid-delta (button LIVE), closes by
# tool (sealed tail), and a SECOND reasoning with ITS own message.
_STAGES = [
    _ev(1, "reasoning_delta", "x" * 2000),
    _ev(2, "reasoning_delta", "y" * 1500),   # total 3500 → BOX 1 FULL here
    _ev(3, "tool_input_start"), _ev(4, "tool_call"),  # closes: tail = box 2
    _ev(5, "reasoning_delta", "z" * 300),    # 2nd reasoning…
    _ev(6, "text_delta", "the answer"),      # …closes on writing: box 2.1
]


class TestBoxesBySize(_Base):
    async def test_each_box_its_own_message_born_at_start_frozen_at_the_seal(self):
        v = self._view(support.FakeEventsClient(list(_STAGES)))
        cot, text, steps, live_id = v.run()
        # F57: every box has its OWN message, born text-only at its START
        # (anti-silence: the first one with the very first delta) — one
        # per box: 1.1, 1.2 (as soon as 1.1 seals) and 2.1
        self.assertEqual(len(self.sends), 3)
        self.assertIn("filling CoT 1.1 (2000 chars)", self.sends[0][0])
        self.assertIn("filling CoT 1.2 (500 chars)", self.sends[1][0])
        self.assertIn("filling CoT 2.1 (300 chars)", self.sends[2][0])
        self.assertFalse(any(s[1] for s in self.sends))  # born WITHOUT keyboard
        # the sealed texts: box 1 = first 3000 chars, box 2 = 500 tail
        self.assertEqual(v.cot_boxes[0], "x" * 2000 + "y" * 1000)
        self.assertEqual(v.cot_boxes[1], "y" * 500)
        # the seal FREEZES the message with ITS single button — box 1.1
        # fills MID-REASONING (2nd delta, before the tool): its button
        # travels right there, not at the close of the phase
        frees = [e for e in self.edits if e[2]]
        self.assertEqual([self._buttons(e[2]) for e in frees],
                         [["CoT 1.1"], ["CoT 1.2"], ["CoT 2.1"]])
        self.assertIn("<b>CoT 1.1</b>", frees[0][1])
        self.assertEqual(frees[0][2]["inline_keyboard"][0][0]
                         ["callback_data"].split(":")[-1], "1")
        # F57: no round messages — the boxes themselves are the timeline
        self.assertFalse(any("pass " in e[1] for e in self.edits))
        self.assertFalse(any("Reasoning" in e[1] for e in self.edits))
        self.assertEqual(v.cot_labels, ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertTrue(all(v.cot_phs))  # every box reachable through its button
        # the tap opens box 3 (the 2nd reasoning) via the SAME path as the Mac
        out = reasoning.show(self.T1, int(v.t0), 3)
        self.assertIn("<pre>%s</pre>" % ("z" * 300), out)
        self.assertIn("🧠 <b>CoT 2.1</b>", out)  # the label of ITS reasoning

    async def test_cot_stream_text_is_not_sent(self):
        v = self._view(support.FakeEventsClient(list(_STAGES)),
                       cfg=_cfg(self.zt, cot_buttons=True, cot_stream=True))
        v.run()
        self.assertFalse(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any("analysis done" in s[0] for s in self.sends))
        # cot_full stays WHOLE (nothing is released): the boxes cover all of it
        self.assertEqual("".join(v.cot_boxes),
                         "x" * 2000 + "y" * 1500 + "z" * 300)

    async def test_knob_off_behaves_as_before(self):
        v = self._view(support.FakeEventsClient(list(_STAGES)),
                       cfg=_cfg(self.zt, cot_buttons=False, cot_stream=True))
        v.run()
        # the CoT travels as text (permanent boxes) and there are no buttons
        self.assertTrue(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any(s[1] for s in self.sends))

    async def test_pass_below_the_cap_one_box_at_close(self):
        v = self._view(support.FakeEventsClient([_ev(1, "reasoning_delta", "corto"),
                               _ev(2, "text_delta", "resp")]))
        v.run()
        self.assertEqual(v.cot_boxes, ["corto"])
        self.assertEqual(v.cot_labels, ["CoT 1.1"])
        # F57: the box's message was born at its start and FREEZES at the
        # close (the seal of the tail) with ITS single button
        self.assertIn("filling CoT 1.1 (5 chars)", self.sends[0][0])
        final = [e for e in self.edits if e[2]][-1]
        self.assertIn("<b>CoT 1.1</b>", final[1])
        self.assertEqual(self._buttons(final[2]), ["CoT 1.1"])

    async def test_the_live_box_shows_status_not_raw_text(self):
        """F43b — with buttons the CoT is NOT seen twice: the live box says
        how many boxes there are and which one is filling; the content
        lives behind the taps. Fake clock (10s per now()) so the edit's
        3s gate always passes, and events by STAGES like a real turn
        (think first, write afterwards)."""

        class _ByStage:
            def __init__(self, stages):
                self.stages, self.i = stages, 0
                self.requests = []

            def request(self, method, params=None, timeout=None):
                self.requests.append(method)
                f = min(self.i, len(self.stages) - 1)
                evs = [dict(e) for e in self.stages[f]]
                self.i += 1
                return {"result": {"events": evs}}

        long_text = "r" * (CAP + 400)
        stages = [[_ev(1, "reasoning_delta", "I think "),
                   _ev(2, "reasoning_delta", long_text)],
                  [_ev(1, "reasoning_delta", "I think "),
                   _ev(2, "reasoning_delta", long_text),
                   _ev(3, "text_delta", "reply")]]
        # R12: shared fake clock (10s per now(), no sleep pacing)
        # so the edit's 3s gate always passes
        v = self._view(_ByStage(stages),
                       now=support.FakeClock(step_s=10.0,
                                             with_sleep=False).time)
        v.th = support.FakeThread(4)
        v.run()
        live_lines = [tx for _m, tx, _mk in self.edits if "thinking" in tx]
        self.assertTrue(live_lines)
        self.assertTrue(any("1 box · filling CoT 1.2" in x for x in live_lines))
        for x in live_lines:
            self.assertNotIn("<pre>", x)   # without the raw CoT text
            self.assertNotIn("rrrr", x)
        # with the knob off, the live box shows the text as always
        v2 = self._view(_ByStage([[_ev(1, "reasoning_delta", "thinking something")],
                                  [_ev(1, "reasoning_delta", "thinking something"),
                                   _ev(2, "text_delta", "reply")]]),
                        cfg=_cfg(self.zt, cot_buttons=False),
                        now=support.FakeClock(step_s=10.0,
                                              with_sleep=False).time)
        v2.th = support.FakeThread(4)
        v2.run()
        live_lines2 = [tx for _m, tx, _mk in self.edits if "thinking:" in tx]
        self.assertTrue(any("thinking something" in x for x in live_lines2))

    async def test_turn_without_reasoning_creates_no_box(self):
        v = self._view(support.FakeEventsClient([_ev(1, "text_delta", "direct")]))
        v.run()
        self.assertFalse(any(s[1] for s in self.sends))
        self.assertFalse(os.path.exists(reasoning.cache_path(self.T1, int(v.t0))))


class TestTelemetryCotMsgs(_Base):
    """The «live fin» line of the pwlog: cot_msgs counted ONLY the text
    chunks from _cot_flush — which returns early in button mode — so in
    production it always read 0 even when the turn had published boxes
    (20/10 19:09: message 5499 with CoT 1.1/1.2 and `cot_msgs=0`). Now
    it counts the box messages BORN (edits don't add up) and the line
    carries boxes= to read the turn at a glance."""

    def _logs(self):
        logs = []
        return logs, (lambda *a: logs.append(" ".join(str(x) for x in a)))

    async def test_counts_born_messages_not_edits(self):
        logs, pwlog = self._logs()
        v = self._view(support.FakeEventsClient(list(_STAGES)), pwlog=pwlog)
        v.run()
        # _STAGES: 3 boxes, each with its OWN message born at its start
        # (one per BOX now, not per round — the freeze is an EDIT of the
        # same message and doesn't add up)
        self.assertEqual(v.cot_nmsg, 3)
        self.assertEqual(len(self.sends), 3)   # one birth per box, text-only
        self.assertFalse(any(s[1] for s in self.sends))
        fin_line = [ln for ln in logs if ln.startswith("live fin:")][0]
        self.assertIn("cot_msgs=3", fin_line)
        self.assertIn("boxes=3", fin_line)

    async def test_boxes_without_message_are_lost_not_landed(self):
        # F75 — the reasonings' sends FAIL (no message_id): per the
        # live-or-lost contract (Damian, 27/10) the boxes that never got
        # their message never travel — no latecomer bundle at the close;
        # the cache keeps every box for /cot
        logs, pwlog = self._logs()
        support.patch(self, reasoning, "pwlog", pwlog)  # kb_log writes here
        calls = []

        def send(cfg, text, loud=None, markup=None):
            calls.append((text, markup))
            return None
        v = self._view(support.FakeEventsClient(list(_STAGES)), send=send, pwlog=pwlog)
        v.run()
        self.assertEqual(v.cot_nmsg, 0)
        self.assertFalse([c for c in calls if "as the turn closed" in c[0]])
        fin_line = [ln for ln in logs if ln.startswith("live fin:")][0]
        self.assertIn("cot_msgs=0 boxes=3", fin_line)

    async def test_text_mode_still_counts_the_chunks(self):
        logs, pwlog = self._logs()
        v = self._view(support.FakeEventsClient(list(_STAGES)), pwlog=pwlog,
                       cfg=_cfg(self.zt, cot_buttons=False, cot_stream=True))
        v.run()
        chunks = [s for s in self.sends if "· CoT " in s[0]]
        self.assertGreater(len(chunks), 0)
        self.assertEqual(v.cot_nmsg, len(chunks))
        fin_line = [ln for ln in logs if ln.startswith("live fin:")][0]
        self.assertIn("cot_msgs=%d" % len(chunks), fin_line)
        self.assertIn("boxes=0", fin_line)  # without buttons there are no sealed boxes


class TestNumberingWithoutHolesOrMerges(_Base):
    """F48c — what the 20/10 e2e uncovered (a 32-min turn: «CoT 34.3»
    with 19 real reasonings; the next one: 17 boxes in 2 reasonings). Two
    defects: (1) after closing by tool, self.reason stayed full and every
    following tool re-closed an EMPTY reasoning, consuming an R number;
    (2) text_phase_noted wasn't reset → from the first write on, a reasoning
    followed by TEXT didn't close and the next one merged into it."""

    async def test_four_real_passes_are_four(self):
        evs = [
            _ev(1, "reasoning_delta", "a" * 100), _ev(2, "text_delta", "t1"),
            _ev(3, "reasoning_delta", "b" * 100), _ev(4, "text_delta", "t2"),
            _ev(5, "reasoning_delta", "c" * 100), _ev(6, "tool_input_start"),
            _ev(7, "tool_input_start"), _ev(8, "tool_input_start"),
            _ev(9, "reasoning_delta", "d" * 100), _ev(10, "text_delta", "t3"),
        ]
        v = self._view(support.FakeEventsClient(evs))
        v.run()
        # before: ['CoT 1.1', 'CoT 2.1', 'CoT 5.1'] with box 2 = b+c
        self.assertEqual(v.cot_labels,
                         ["CoT 1.1", "CoT 2.1", "CoT 3.1", "CoT 4.1"])
        self.assertEqual(v.cot_boxes, ["a" * 100, "b" * 100, "c" * 100,
                                       "d" * 100])
        # F57: one message PER BOX, born at each box's start
        self.assertEqual(len(self.sends), 4)
        self.assertFalse(any(s[1] for s in self.sends))  # births: no keyboard
        self.assertEqual(v.cot_nmsg, 4)

    async def test_back_to_back_tools_after_thinking_consume_no_numbers(self):
        evs = [
            _ev(1, "reasoning_delta", "a" * 50),
            _ev(2, "tool_input_start"), _ev(3, "tool_call"),
            _ev(4, "tool_input_start"), _ev(5, "tool_call"),
            _ev(6, "tool_input_start"), _ev(7, "tool_call"),
            _ev(8, "reasoning_delta", "b" * 50), _ev(9, "tool_input_start"),
        ]
        v = self._view(support.FakeEventsClient(evs))
        v.run()
        self.assertEqual(v.cot_labels, ["CoT 1.1", "CoT 2.1"])
        self.assertEqual(v.cot_round, 3)  # the next one would be 3, not 6

    async def test_text_mode_phase_notice_stays_one(self):
        # the text_phase_noted guard remains ONLY for the text-mode notice
        evs = [
            _ev(1, "reasoning_delta", "a" * 50), _ev(2, "text_delta", "t1"),
            _ev(3, "reasoning_delta", "b" * 50), _ev(4, "text_delta", "t2"),
        ]
        v = self._view(support.FakeEventsClient(evs),
                       cfg=_cfg(self.zt, cot_buttons=False, cot_stream=True))
        v.run()
        notices = [s for s in self.sends if "analysis done" in s[0]]
        self.assertEqual(len(notices), 1)


if __name__ == "__main__":
    unittest.main()
