# -*- coding: utf-8 -*-
"""F8b — session rotation: transcript above threshold → compact +
summary turn + seeded new session (bot-active swap, registry and queue
rekey, model restore for the old one, chat notice). Below the
threshold nothing happens; create/seed failure → continuity on the old
session; /stop during the dance → aborts without touching anything."""
import unittest
from unittest import mock

import support
from test_prompt_worker import _cfg, _FakeC, _ViewRapida

from zcode_bot import workers as zc_workers
from zcode_bot.utils import http as zc_http

SID2 = "sess_rotada-1111"


class _FakeCreator(_FakeC):
    """New runtime that knows how to create a session (the rotation)."""

    def __init__(self, new_sid=SID2):
        super().__init__()
        self.new_sid = new_sid

    def request(self, method, params=None, timeout=None):
        if method == "session/create":
            self.requests.append((method, params))
            return {"result": {"session": {"sessionId": self.new_sid}}}
        return super().request(method, params, timeout)


class _CreateDenied(_FakeC):
    """New runtime whose session creation is denied (create failure)."""

    def request(self, method, params=None, timeout=None):
        if method == "session/create":
            return {"error": {"code": -32000, "message": "boom"}}
        return super().request(method, params, timeout)


class TestRotation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        self.deliveries = []
        self.cs = []
        self.mbs = {"v": 20.0}
        self.restored = []
        self.crea_cls = _FakeCreator

        def make(cwd=None, **kw):
            c = _FakeC() if not self.cs else self.crea_cls()
            self.cs.append(c)
            return c

        def fetch(c, sid, after_id=None, limit=40):
            return [{"info": {"role": "assistant", "id": "m2"},
                     "parts": [{"type": "text",
                                "text": "ESTADO: resumen de continuidad"}]}]

        patches = [
            mock.patch.object(zc_http, "send",
                              lambda cfg, text, loud=None, markup=None:
                              (self.sends.append(text), True)[1]),
            mock.patch.object(zc_http, "_live_send", lambda cfg, text: 555),
            mock.patch.object(zc_http, "_live_edit",
                              lambda cfg, mid, text: True),
            mock.patch.object(zc_workers, "zc_proto",
                              type("M", (), {
                                  "make": staticmethod(make),
                                  "proto_err": __import__(
                                      "zcode_bot.proto",
                                      fromlist=["proto_err"]).proto_err})),
            mock.patch.object(zc_workers, "_turn_end_poll",
                              lambda *a, **k: {"event": "turn.completed"}),
            mock.patch.object(zc_workers, "_wait_cli_event",
                              lambda *a, **k: {"event": "compact.completed"}),
            mock.patch.object(zc_workers, "_fetch_messages", fetch),
            mock.patch.object(zc_workers, "_transcript_mb",
                              lambda sid: self.mbs["v"]),
            mock.patch.object(zc_workers, "_restore_model",
                              lambda sid, snap:
                              self.restored.append((sid, snap))),
            mock.patch.object(zc_workers, "LiveTurnView", _ViewRapida),
            mock.patch.object(zc_workers, "_send_long_reply",
                              lambda cfg, title, dur, text, max_parts=3:
                              self.deliveries.append((title, text))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_rotates_above_threshold(self):
        zc_workers.prompt_worker(
            _cfg(self.zt, warm_keepalive_s=1, rotate_threshold_mb=8),
            self.act, "hola")
        # the full dance: compact and summary turn on the old one,
        # seed on the new one
        old_methods = [m for m, _p in self.cs[0].requests]
        self.assertIn("session/compact", old_methods)
        self.assertTrue(any("Write the continuity summary"
                            in p.get("content", "")
                            for m, p in self.cs[0].requests
                            if m == "session/send"))
        new_methods = [m for m, _p in self.cs[1].requests]
        self.assertIn("session/create", new_methods)
        seed_params = [p for m, p in self.cs[1].requests
                       if m == "session/send"]
        self.assertTrue(any("resumen de continuidad" in p.get("content", "")
                            for p in seed_params))
        # swap: the active session is the new one
        self.assertEqual(zc_workers.active_bot_session().get("sid"), SID2)
        # the old one was restored with ITS snapshot and its runtime closed
        self.assertEqual(len(self.restored), 1)
        self.assertEqual(self.restored[0][0], self.T2)
        self.assertTrue(self.cs[0].closed)
        # notice to the chat
        self.assertTrue(any("rotated" in s for s in self.sends))

    async def test_below_threshold_no_rotation(self):
        self.mbs["v"] = 1.0
        zc_workers.prompt_worker(
            _cfg(self.zt, warm_keepalive_s=1, rotate_threshold_mb=8),
            self.act, "hola")
        self.assertEqual(len(self.cs), 1)
        self.assertNotIn("session/compact",
                         [m for m, _p in self.cs[0].requests])
        self.assertFalse(any("♻️" in s for s in self.sends))
        self.assertEqual(zc_workers.active_bot_session().get("sid"),
                         self.T2)

    async def test_create_failure_stays_on_the_old_one(self):
        self.crea_cls = _CreateDenied
        zc_workers.prompt_worker(
            _cfg(self.zt, warm_keepalive_s=1, rotate_threshold_mb=8),
            self.act, "hola")
        # it tried (2nd runtime) but came back: no swap, no rotation
        # notice, and the discarded runtime was left closed
        self.assertEqual(len(self.cs), 2)
        self.assertTrue(self.cs[1].closed)
        self.assertEqual(zc_workers.active_bot_session().get("sid"),
                         self.T2)
        self.assertFalse(any("rotated" in s for s in self.sends))
        self.assertTrue(any("couldn't create the new session" in s for s in self.sends))
        self.assertTrue(self.cs[0].closed)  # the warm phase still ended clean

    async def test_stop_during_the_dance_touches_nothing(self):
        c = _FakeC()
        zc_workers._stop_request(self.T2)
        try:
            res = zc_workers._rotate_session(
                c, _cfg(self.zt), self.act, self.T2, support.PROJ_CV,
                "CV", None, None)
            self.assertIsNone(res)
            self.assertNotIn("session/compact",
                             [m for m, _p in c.requests])
        finally:
            zc_workers._stop_consume(self.T2)

    async def test_message_after_compact_defers_rotation(self):
        # F28: the queue receives a message while compacting → abort
        # BEFORE the summary turn and without creating a new runtime;
        # the old one stays alive and the swap never happens
        support.patch(self, zc_workers, "_queue_peek",
                        side_effect=[["mensaje"]])
        c = _FakeC()
        before_active = zc_workers.active_bot_session()
        res = zc_workers._rotate_session(
            c, _cfg(self.zt), self.act, self.T2, support.PROJ_CV,
            "CV", None, None)
        self.assertIsNone(res)
        self.assertIn("session/compact", [m for m, _p in c.requests])
        self.assertEqual(self.cs, [])  # phase 3 never started
        self.assertFalse(c.closed)     # the old one stays ALIVE (the caller continues)
        self.assertEqual(zc_workers.active_bot_session(), before_active)
        self.assertFalse(any("rotated" in s for s in self.sends))
        self.assertTrue(any("mid-rotation" in s for s in self.sends))

    async def test_message_after_summary_defers_rotation(self):
        # F28: the summary WENT OUT (full turn on the old one) but a
        # message arrived before creating the new session → abort
        # without swap; the cost is redoing the dance on the next
        # keepalive, never stomping the active session while messages
        # are waiting
        support.patch(self, zc_workers, "_queue_peek",
                        side_effect=[[], ["mensaje"]])
        c = _FakeC()
        before_active = zc_workers.active_bot_session()
        res = zc_workers._rotate_session(
            c, _cfg(self.zt), self.act, self.T2, support.PROJ_CV,
            "CV", None, None)
        self.assertIsNone(res)
        self.assertIn("session/compact", [m for m, _p in c.requests])
        self.assertTrue(any("Write the continuity summary" in p.get("content", "")
                            for m, p in c.requests if m == "session/send"))
        self.assertEqual(self.cs, [])  # the new session was never created
        self.assertFalse(c.closed)
        self.assertEqual(zc_workers.active_bot_session(), before_active)
        self.assertFalse(any("rotated" in s for s in self.sends))

    async def test_queue_rekey_moves_the_queued_items(self):
        zc_workers._queue_put(self.T2, self.act, "m1")
        zc_workers._queue_rekey(self.T2, SID2)
        self.assertEqual(self.zt._queue_peek(self.T2), [])
        self.assertEqual(len(self.zt._queue_peek(SID2)), 1)
        zc_workers._queue_rekey(SID2, SID2)  # noop without a crash
        zc_workers._queue_rekey(SID2, None)  # noop without a crash


if __name__ == "__main__":
    unittest.main()
