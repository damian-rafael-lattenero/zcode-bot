"""F9 — the turn end arrives via the event STREAM (ADR-013 follow-up).

_turn_end_poll stops scanning JSONL logs when there is a connection +
cursor: incremental afterSeq poll with correlation by inputId (the
stream doesn't carry turnId — spike scripts/spike_f9_turnfin.py). Log
scanning remains as the fallback (dead conn, giant frame, no conn). The
F20 regression is included: a FOREIGN turn.completed (another inputId)
is never accepted.
"""
import json
import os
import time
import unittest

import support
from zcode_bot import config as zc_config
from zcode_bot import live_view as zc_live


class FakeConn:
    """Fake ProtoClient: scripted session/events responses."""

    def __init__(self, batches=None, error=None):
        self.batches = list(batches or [])
        self.error = error
        self.params = []

    def request(self, method, params=None, timeout=200):
        assert method == "session/events", method
        self.params.append(dict(params or {}))
        if self.error:
            raise self.error
        if self.batches:
            return {"result": {"events": self.batches.pop(0)}}
        return {"result": {"events": []}}


def ev_turn(ev_type, input_id, ts_ms=None, extra=None):
    p = {"inputId": input_id}
    if ts_ms is not None:
        p["executionStartedAt"] = ts_ms
    if extra:
        p.update(extra)
    return {"type": ev_type, "payload": p, "seq": int(time.time() * 1000) % 10 ** 6,
            "eventId": "e-%s-%f" % (input_id, time.time())}


class TestFinDeStream(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.t_send = time.time() - 1

    def test_end_correlated_by_input_id(self):
        # our started (inputId A) → a foreign B completes → completed A
        conn = FakeConn(batches=[[
            ev_turn("turn.started", "A", ts_ms=self.t_send * 1000),
            ev_turn("turn.started", "B", ts_ms=self.t_send * 1000),
            ev_turn("turn.completed", "B", extra={"resultType": "success"}),
            ev_turn("turn.completed", "A", extra={"resultType": "success"}),
        ]])
        end_ev = self.zt._turn_end_poll(support.T1, time.time() - 30, timeout=5,
                                     t_send=self.t_send, conn=conn,
                                     start_seq=10)
        self.assertIsNotNone(end_ev)
        self.assertEqual(end_ev["event"], "turn.completed")
        # the first poll was incremental (afterSeq from the cursor)
        self.assertEqual(conn.params[0].get("afterSeq"), 10)

    def test_without_t_send_accepts_anyone(self):
        conn = FakeConn(batches=[[
            ev_turn("turn.started", "X", ts_ms=1),
            ev_turn("turn.completed", "X", extra={"resultType": "success"}),
        ]])
        end_ev = self.zt._turn_end_poll(support.T1, time.time() - 30, timeout=5,
                                     conn=conn, start_seq=0)
        self.assertIsNotNone(end_ev)
        self.assertEqual(end_ev["event"], "turn.completed")

    def test_result_type_failed_is_turn_failed(self):
        conn = FakeConn(batches=[[
            ev_turn("turn.started", "A", ts_ms=self.t_send * 1000),
            ev_turn("turn.completed", "A",
                    extra={"resultType": "error",
                           "error": {"message": "model boom"}}),
        ]])
        end_ev = self.zt._turn_end_poll(support.T1, time.time() - 30, timeout=5,
                                     t_send=self.t_send, conn=conn,
                                     start_seq=0)
        self.assertEqual(end_ev["event"], "turn.failed")
        self.assertIn("model boom", end_ev["error"]["message"])

    def test_foreign_completed_without_mine_is_not_accepted(self):
        # F20 in the stream: a peer's completed WITHOUT having seen our
        # started → doesn't count (with t_send, mine=None accepts nothing)
        conn = FakeConn(batches=[[
            ev_turn("turn.completed", "AJENO",
                    extra={"resultType": "success"})]])
        end_ev = self.zt._turn_end_poll(support.T1, time.time() - 30,
                                     timeout=0.4, t_send=self.t_send,
                                     conn=conn, start_seq=0)
        self.assertIsNone(end_ev)

    def test_stream_down_degrades_to_logs(self):
        # conn blows up on the first poll → fallback: the end comes from
        # the CLI log (F20 path intact, with turnId)
        log_hoy = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        base = time.time() - 30
        with open(log_hoy, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.started",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG"}) + "\n")
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.completed",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG"}) + "\n")
        conn = FakeConn(error=OSError("pipe roto"))
        end_ev = self.zt._turn_end_poll(support.T1, base, timeout=5,
                                     t_send=base, conn=conn, start_seq=0)
        self.assertIsNotNone(end_ev)
        self.assertEqual(end_ev.get("turnId"), "turn_LOG")

    def test_giant_frame_degrades_to_logs(self):
        # broken afterSeq (a 26MB replay in one incremental poll) → same
        # anti-OOM trip as live: degrade to logs, don't parse GBs
        log_hoy = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        base = time.time() - 30
        with open(log_hoy, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.started",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG2"}) + "\n")
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.completed",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG2"}) + "\n")
        conn = FakeConn(batches=[{"result": {"events": [], "pad": "x" * (self.zt.LIVE_FRAME_TRIP + (1 << 20))}}])
        end_ev = self.zt._turn_end_poll(support.T1, base, timeout=8,
                                     t_send=base, conn=conn, start_seq=0)
        self.assertIsNotNone(end_ev)
        self.assertEqual(end_ev.get("turnId"), "turn_LOG2")


class TestFinDeStreamBordes(unittest.TestCase):
    """Edges of the PURE correlator (no conn, no fixture): the
    started/completed pair can be identified by queryId (no inputId) at
    either end, and the payload error can arrive as a string, not a
    dict."""

    T_SEND = time.time() - 5

    def _started(self, **pid):
        p = {"executionStartedAt": (time.time() - 4) * 1000}
        p.update(pid)
        return {"type": "turn.started", "seq": 11, "payload": p}

    def test_fallback_query_id_on_both_ends(self):
        # runtime that identifies the turn by queryId (no inputId):
        # started sets mine from queryId and the completed with the SAME
        # queryId closes it — before, matching assumed inputId and that
        # end was never seen
        completed = {"type": "turn.completed", "seq": 12,
                     "payload": {"queryId": "q-77",
                                 "resultType": "success"}}
        end_ev, mine, last = zc_live._stream_end(
            [self._started(queryId="q-77"), completed], None,
            self.T_SEND, 0)
        self.assertIsNotNone(end_ev)
        self.assertEqual(end_ev["event"], "turn.completed")
        self.assertEqual(mine, "q-77")
        self.assertEqual(last, 12)

    def test_error_as_string_is_turn_failed(self):
        # raw error (string) instead of {message: ...}: the end message
        # is that string, not a crash or an empty dict
        completed = {"type": "turn.completed", "seq": 12,
                     "payload": {"inputId": "A", "resultType": "error",
                                 "error": "quota agotada"}}
        end_ev, mine, _ = zc_live._stream_end(
            [self._started(inputId="A"), completed], None,
            self.T_SEND, 0)
        self.assertEqual(end_ev["event"], "turn.failed")
        self.assertEqual(mine, "A")
        self.assertIn("quota agotada", end_ev["error"]["message"])

    def test_failed_without_error_reports_the_result_type(self):
        completed = {"type": "turn.failed", "seq": 12,
                     "payload": {"inputId": "A", "resultType": "cancelled"}}
        end_ev, _mios, _ = zc_live._stream_end(
            [self._started(inputId="A"), completed], None,
            self.T_SEND, 0)
        self.assertEqual(end_ev["event"], "turn.failed")
        self.assertEqual(end_ev["error"]["message"], "cancelled")


if __name__ == "__main__":
    unittest.main()
