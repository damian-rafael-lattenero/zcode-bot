# -*- coding: utf-8 -*-
"""LiveTurnView test battery (F4d) — the live view of the turn.

It used to be 242 lines with 2 tests (only the F15 pause). Here the rest
of the contract is frozen: streaming of deltas with dedupe by eventId,
CoT flush with live-box relocation, anti-OOM guards (giant frame, replay
trip), /stop exactly ONCE, close without pace, and adaptive cadence.

All with injected deps: zero network, zero patched time.sleep.
"""
import unittest
from unittest import mock

import support

from zcode_bot.live_view import (LiveTurnView, COT_TAIL, EVENTS_TRIP,
                                 LIVE_BIG_FRAME, LIVE_FRAME_TRIP)


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


# R13: the local _FakeC/_Th moved to support (FakeEventsClient/FakeThread,
# identical shape) — the call sites were renamed in place.


def _ev(eid, kind, delta):
    return {"eventId": eid, "payload": {"kind": kind, "delta": delta}}


_TOOL_STREAM = ("tool_input_start", "tool_input_delta", "tool_input_end",
                "tool_call")


def _evr(seq, kind, **payload):
    """Event with the REAL protocol shape (spike F7a): dense seq,
    uuid eventId, envelope type (model.streaming / tool.updated)."""
    p = {"kind": kind}
    p.update(payload)
    ev_type = "tool.updated" if kind in ("started", "result", "batch") \
        else "model.streaming"
    return {"eventId": "e-%d" % seq, "seq": seq, "type": ev_type, "payload": p}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def _view(self, c, th=None, cfg=None, **dep_extra):
        """View with recorders: sends, edits, live_sends, skips, stops."""
        self.sends = []
        self.edits = []
        self.live_sends = []
        self.skips = []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append(text), True)[1],
            "live_send": lambda cfg, text:
                (self.live_sends.append(text), 550 + len(self.live_sends))[1],
            "live_edit": lambda cfg, mid, text, markup=None:
                (self.edits.append((mid, text)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: self.skips.append(sid),
        }
        deps.update(dep_extra)
        import time as _t
        return LiveTurnView(c, cfg or _cfg(self.zt), self.T1, _t.time(),
                            "testing", 777, th or support.FakeThread(2), last_id=None,
                            deps=deps)


class TestStreaming(_Base):
    async def test_deltas_edit_the_box_and_the_phase_event(self):
        evs = [_ev(1, "reasoning_delta", "let me think "),
               _ev(2, "text_delta", "writing the reply")]
        v = self._view(support.FakeEventsClient(evs))
        cot, text, steps, live_id = v.run()
        # the think→write phase event sent its own message
        self.assertTrue(any("analysis done" in s for s in self.sends))
        # the live box edits with the writing phase
        self.assertTrue(any("writing" in t for _m, t in self.edits))
        # the dedupe by eventId works: the replay was re-sent 2 times but
        # the counters see each delta ONCE
        self.assertEqual(v.n_deltas, 2)
        self.assertTrue(text.endswith("writing the reply"))
        self.assertEqual(steps, [])
        # the CLOSE releases ALL the remaining CoT (however small) as a
        # permanent message and the live box moves — the flush contract
        self.assertTrue(any("CoT 1" in s for s in self.sends))
        self.assertNotEqual(live_id, 777)
        self.assertEqual(cot, "")  # delivered to the chat = released

    async def test_steps_accumulate_with_own_box(self):
        msgs = [{"info": {"role": "assistant", "id": "m9"}, "parts": [
            {"type": "tool", "tool": "Bash",
             "state": {"input": {"command": ["ls"]}}}]}]
        # steps_boxes injected: without it the test fell to the REAL
        # _boxes_new_step — live uhttp.send to api.telegram.org plus its
        # pacing sleeps, ~6 s of real network hidden under the suite
        perm = []
        v = self._view(support.FakeEventsClient(),
                       fetch_messages=lambda *a, **k: msgs,
                       steps_boxes=lambda cfg, t, counter, new_items,
                       boxed=None: perm.append(new_items))
        cot, text, steps, live_id = v.run()
        self.assertEqual([p for p in steps], [("Bash", "ls")])
        # the steps box was born (live_send) and got edited with the count
        self.assertTrue(any("steps:" in s for s in self.live_sends))
        self.assertTrue(any("1 step" in t for _m, t in self.edits))
        # and the permanent box WAS offered (the seam reached, no network)
        self.assertEqual([i[1] for i in perm[0]], ["Bash"])


class TestCotFlush(_Base):
    async def test_flush_moves_the_live_box_and_close_sends_the_rest(self):
        evs = [_ev(1, "reasoning_delta", "R" * 5000)]
        v = self._view(support.FakeEventsClient(evs))
        cot, _text, _steps, live_id = v.run()
        # CoT 1 (3000 chars) went out as a permanent message in the loop…
        self.assertTrue(any("CoT 1" in s for s in self.sends))
        # …the live box MOVED at the end (live_send new id ≠ 777)…
        self.assertNotEqual(live_id, 777)
        # …and the close without pace sent the rest as CoT 2
        self.assertTrue(any("CoT 2" in s for s in self.sends))
        # what was delivered to the chat got RELEASED: nothing stays held in memory
        self.assertEqual(cot, "")

    async def test_cot_stream_off_sends_nothing(self):
        evs = [_ev(1, "reasoning_delta", "R" * 5000)]
        v = self._view(support.FakeEventsClient(evs), cfg=_cfg(self.zt, cot_stream=False))
        cot, _t, _p, live_id = v.run()
        self.assertEqual(self.sends, [])
        self.assertEqual(live_id, 777)  # it never moved
        self.assertEqual(len(cot), 5000)  # the CoT stays for the trace


class TestAntiOOM(_Base):
    """F7+++ — the trips guard the FOLLOWING polls (the first one may
    bring the COMPLETE replay ONCE: it gets paid, shown, released).
    The frame is measured PER POLL (the max_frame inherited from the
    resume was killing healthy streams: 'evs=0 frame=25MB')."""

    async def test_repeated_giant_frame_turns_off_and_marks(self):
        # broken afterSeq: EVERY poll brings 26MB → the first one is
        # tolerated, the SECOND turns it off and marks the session
        from zcode_bot import live_view as zc_live
        with mock.patch.object(zc_live, "_frame_size",
                               lambda r: LIVE_FRAME_TRIP + 1):
            c = support.FakeEventsClient()
            v = self._view(c, th=support.FakeThread(4))
            v.run()
        self.assertTrue(v.events_off)
        self.assertEqual(self.skips, [self.T1])
        self.assertGreaterEqual(c.requests.count("session/events"), 2)

    async def test_giant_frame_only_on_the_first_does_not_turn_off(self):
        # startup replay a single time (small incremental afterwards):
        # the CoT is shown IN FULL — no tripping (Damian's request)
        from zcode_bot import live_view as zc_live
        sizes = [LIVE_FRAME_TRIP + 1, 10, 10]
        with mock.patch.object(zc_live, "_frame_size",
                               lambda r: sizes.pop(0) if sizes else 10):
            c = support.FakeEventsClient()
            v = self._view(c, th=support.FakeThread(4))
            v.run()
        self.assertFalse(v.events_off)
        self.assertEqual(self.skips, [])

    async def test_replay_trip_turns_off_without_marking(self):
        # 1300 small events on EVERY poll (replay): the first one is
        # tolerated, the second trips EVENTS_TRIP without marking the session
        evs = [_ev(i, "reasoning_delta", "x") for i in range(EVENTS_TRIP + 100)]
        c = support.FakeEventsClient(evs)
        v = self._view(c, th=support.FakeThread(4))
        v.run()
        self.assertTrue(v.events_off)
        self.assertEqual(self.skips, [])  # it isn't the session: it's this turn
        self.assertGreaterEqual(c.requests.count("session/events"), 2)

    async def test_adaptive_cadence_for_large_frame(self):
        from zcode_bot import live_view as zc_live
        with mock.patch.object(zc_live, "_frame_size",
                               lambda r: LIVE_BIG_FRAME + 1):
            v = self._view(support.FakeEventsClient(), th=support.FakeThread(2))
            v.run()
        self.assertEqual(v.cad, 5.0)  # 2s normal, 5s with a frame >2MB


class TestStop(_Base):
    async def test_stop_requests_session_stop_ONCE(self):
        c = support.FakeEventsClient()
        v = self._view(c, th=support.FakeThread(4),
                       stop_requested=lambda sid: True)
        v.run()
        self.assertEqual(c.requests.count("session/stop"), 1)


class TestAfterSeq(_Base):
    """F7b — incremental polling: the request carries afterSeq from the
    start (eventSeq from the subscribe) and the cursor advances with the
    highest seq seen."""

    async def test_cursor_and_afterseq_on_every_poll(self):
        evs = [_evr(1, "reasoning_delta", delta="thinking "),
               _evr(2, "reasoning_delta", delta="stuff"),
               _evr(3, "text_delta", delta="replying")]
        c = support.FakeEventsClient(evs)
        v = self._view(c, th=support.FakeThread(3))
        v.last_seq = 0  # as if the subscribe had returned eventSeq=0
        v.run()
        polls = [p for m, p in c.req_params if m == "session/events"]
        self.assertTrue(polls)  # there were polls
        self.assertTrue(all(p.get("afterSeq") == 0 for p in polls[:1]))
        # the cursor stayed at the highest seq seen
        self.assertEqual(v.last_seq, 3)
        # dedupe: the re-sent replay didn't duplicate deltas
        self.assertEqual(v.n_deltas, 3)

    async def test_without_seq_behaves_as_before(self):
        # old fixtures without seq: afterSeq is never sent (tolerant)
        evs = [_ev(1, "reasoning_delta", "x")]
        c = support.FakeEventsClient(evs)
        v = self._view(c, th=support.FakeThread(2))
        v.run()
        polls = [p for m, p in c.req_params if m == "session/events"]
        self.assertTrue(all("afterSeq" not in p for p in polls))


class TestLiveAction(_Base):
    """F7c — a tool's life cycle as seen by the stream: a box that is
    born 🔵 (tool_call, input already parsed), gets edited to ✅/❌ with
    tool.updated.result, and the command is seen being typed in the
    live box."""

    def _tool_evs(self, until="result", ok=True):
        evs = [_evr(1, "tool_input_start", toolCallId="c1", toolName="Bash"),
               _evr(2, "tool_input_delta", toolCallId="c1",
                    delta='{"command":"echo ho'),
               _evr(3, "tool_input_delta", toolCallId="c1", delta='llo"}'),
               _evr(4, "tool_call", toolCallId="c1", toolName="Bash",
                    input={"command": "echo hello"}),
               _evr(5, "started", toolCallId="c1", toolName="Bash")]
        if until == "result":
            evs.append(_evr(6, "result", toolCallId="c1",
                            result={"success": ok, "content": "hello"}))
        elif until == "error":
            evs.append(_evr(6, "result", toolCallId="c1",
                            result={"success": False,
                                    "content": "boom"}))
        return evs

    async def test_box_born_blue_and_edited_to_green(self):
        v = self._view(support.FakeEventsClient(self._tool_evs()))
        cot, _t, steps, _lid = v.run()
        # the permanent box was born with the full command and shows a 🔵
        self.assertTrue(any("echo hello" in s and "🔵" in s
                            for s in self.live_sends))
        # and the result EDITED it to ✅ with the output (box msg_id = 551)
        self.assertTrue(any(m == 551 and "✅" in t and "hello" in t
                            for m, t in self.edits))
        # the steps index has it instantly (stream, not messages)
        self.assertEqual([p.name for p in steps], ["Bash"])

    async def test_failed_result_marks_red(self):
        v = self._view(support.FakeEventsClient(self._tool_evs(until="error")))
        v.run()
        self.assertTrue(any(m == 551 and "❌" in t and "boom" in t
                            for m, t in self.edits))

    async def test_command_being_typed_in_the_live_box(self):
        # only input_start + one delta: the tool is being typed, no box yet
        evs = [_evr(1, "tool_input_start", toolCallId="c1", toolName="Bash"),
               _evr(2, "tool_input_delta", toolCallId="c1",
                    delta='{"command":"git sta')]
        v = self._view(support.FakeEventsClient(evs))
        v.run()
        self.assertTrue(any("typing" in t and "git sta" in t
                            for _m, t in self.edits))
        # without tool_call no permanent box was born
        self.assertFalse(any("Bash" in s for s in self.live_sends))

    async def test_tools_stream_off_turns_off_live_action(self):
        v = self._view(support.FakeEventsClient(self._tool_evs()),
                       cfg=_cfg(self.zt, tools_stream=False))
        cot, _t, steps, _lid = v.run()
        self.assertFalse(any("Bash" in s for s in self.live_sends))
        self.assertEqual(steps, [])

    async def test_read_only_index_with_action_preset(self):
        # F7c+: 'accion' (default) — Read doesn't spend a permanent box...
        evs = [_evr(1, "tool_call", toolCallId="c1", toolName="Read",
                    input={"file_path": "/x/y.py"})]
        v = self._view(support.FakeEventsClient(evs))
        _cot, _t, steps, _lid = v.run()
        self.assertFalse(any("Read" in s for s in self.live_sends))
        self.assertEqual([p.name for p in steps], ["Read"])  # the index does
        # ...but with tools_box_filter=all the box is born
        v2 = self._view(support.FakeEventsClient(evs),
                        cfg=_cfg(self.zt, tools_box_filter="all"))
        v2.run()
        self.assertTrue(any("Read" in s for s in self.live_sends))

    async def test_message_does_not_duplicate_the_stream_one(self):
        # the history brings the SAME tool (callID c1): the index isn't
        # duplicated and the box isn't re-sent (boxed)
        boxes = []
        msgs = [{"info": {"role": "assistant", "id": "m9"}, "parts": [
            {"type": "tool", "tool": "Bash", "callID": "c1",
             "state": {"input": {"command": "echo hello"},
                       "status": "completed", "output": "hello"}}]}]
        v = self._view(support.FakeEventsClient(self._tool_evs()),
                       fetch_messages=lambda *a, **k: msgs,
                       steps_boxes=lambda cfg, t, counter, new_items, boxed=None:
                       boxes.append((new_items, boxed)))
        cot, _t, steps, _lid = v.run()
        self.assertEqual([p.name for p in steps], ["Bash"])  # ONCE
        # the fallback received the tool with its callID and the boxed set
        self.assertEqual(boxes[0][0][0][0], "c1")
        self.assertIn("c1", boxes[0][1])

    async def test_blue_box_closed_by_history_when_result_missing(self):
        # the stream died midway (events_off/disconnect): the 🔵 box is
        # closed by the messages poll with the output from the history
        msgs = [{"info": {"role": "assistant", "id": "m9"}, "parts": [
            {"type": "tool", "tool": "Bash", "callID": "c1",
             "state": {"input": {"command": "echo hello"},
                       "status": "completed", "output": "hello"}}]}]
        v = self._view(support.FakeEventsClient(self._tool_evs(until="started")),
                       fetch_messages=lambda *a, **k: msgs)
        v.run()
        self.assertTrue(any(m == 551 and "✅" in t and "hello" in t
                            for m, t in self.edits))


class TestAttachResume(_Base):
    """F72 — the /task attach of a MUTED turn (F15 switched it away):
    the mute was one-way and the turn stayed silent until its end. The
    pick reopens the farewell box, lands the boxes sealed through the
    gap as ONE collapsed bundle and the live stream resumes at once."""

    def test_the_pick_reopens_the_stream_and_loses_the_gap(self):
        from zcode_bot import reasoning
        ok = [True]

        class _Staged(support.FakeEventsClient):
            """One NEW delta per poll and the gate flips with them:
            poll 1 live (box 1 seals), poll 2 muted (box 2 seals in
            silence), poll 3 attached again (box 3 seals + a new box
            starts filling — born ON the resume round)."""

            def request(self, method, params=None, timeout=None):
                r = super().request(method, params, timeout)
                if method == "session/events":
                    n = len(self.evs) + 1
                    if n <= 3:
                        size = reasoning.COT_BOX_CAP if n < 3 \
                            else reasoning.COT_BOX_CAP + 700
                        self.evs.append(_evr(n, "reasoning_delta",
                                             delta=str(n) * size))
                    ok[0] = n != 2
                return r

        v = self._view(_Staged(), cfg=_cfg(self.zt, cot_buttons=True,
                                           tools_stream=False),
                       task_current_ok=lambda cfg, sid: ok[0],
                       send=lambda cfg, text, loud=None, markup=None:
                       (self.sends.append((text, markup))
                        or 900 + len(self.sends)),
                       th=support.FakeThread(6))
        v.run()
        # the mute: ONE farewell edit on the turn's own box
        self.assertTrue(any(m == 777 and "moved to the background" in t
                            for m, t in self.edits))
        # the pick: the same box REOPENS — and the gap's boxes never
        # travel (F75: live-or-lost, no collapsed bundle); the cache has
        # them for /cot of the task
        self.assertTrue(any(m == 777 and "back live" in t
                            for m, t in self.edits))
        self.assertFalse([s for s in self.sends
                          if "sealed while you were away" in s[0]])
        # the stream is LIVE again: the next box's message is born on the
        # very round of the resume (delta 3 arrives with the gate back)
        self.assertTrue(any("filling CoT 1.4 (700 chars)" in t
                            for t, _mk in self.sends))
        # the close-time latecomers may carry only POST-resume stragglers
        for t, mk in [s for s in self.sends
                      if "as the turn closed" in s[0]]:
            btns = [b["text"] for row in (mk or {}).get(
                "inline_keyboard", []) for b in row]
            self.assertNotIn("CoT 1.1", btns)
            self.assertNotIn("CoT 1.2", btns)


if __name__ == "__main__":
    unittest.main()
