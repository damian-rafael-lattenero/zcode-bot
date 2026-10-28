# -*- coding: utf-8 -*-
"""F8a — warm worker: pickup of the queued item WITHOUT a new runtime,
clean exit via the /stop marker, timeout with full cleanup, busy Mac →
re-queue and exit, failed warm turn keeps waiting, and knob at 0 = the
classic one-process-per-turn behavior. Reuses the fakes infra from
test_prompt_worker (same contract)."""
import threading
import time
import unittest
from unittest import mock

import support
from test_prompt_worker import _cfg, _FakeC, _QuickView

from zcode_bot import workers as zc_workers
from zcode_bot.utils import http as zc_http


class TestWarmWorker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        self.deliveries = []
        self.c = _FakeC()
        self.makes = []
        patches = [
            mock.patch.object(zc_http, "send",
                              lambda cfg, text, loud=None, markup=None, a_outbox=True:
                              (self.sends.append(text), True)[1]),
            mock.patch.object(zc_http, "_live_send", lambda cfg, text: 555),
            mock.patch.object(zc_http, "_live_edit",
                              lambda cfg, mid, text: True),
            mock.patch.object(zc_workers, "zc_proto",
                              type("M", (), {
                                  "make": staticmethod(
                                      lambda cwd=None, **kw:
                                      (self.makes.append(cwd), self.c)[1]),
                                  "proto_err": __import__(
                                      "zcode_bot.proto",
                                      fromlist=["proto_err"]).proto_err})),
            mock.patch.object(zc_workers, "_turn_end_poll",
                              lambda *a, **k: {"event": "turn.completed"}),
            # same loop, faster ticks: the warm step shrinks 1 s → 50 ms
            mock.patch.object(zc_workers, "_WARM_STEP_S", 0.05),
            mock.patch.object(zc_workers, "LiveTurnView", _QuickView),
            mock.patch.object(zc_workers, "_send_long_reply",
                              lambda cfg, title, dur, text, max_parts=3:
                              self.deliveries.append((title, text))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_warm_pickup_without_new_runtime(self):
        # message #2 arrives while the worker is warm: the SAME runtime
        # takes it (one make, one resume, two deliveries)
        zc_workers._queue_put(self.T2, self.act, "segundo prompt")
        zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=1),
                                 self.act, "primer prompt")
        self.assertEqual(len(self.makes), 1)
        resumes = [m for m, _p in self.c.requests if m == "session/resume"]
        self.assertEqual(len(resumes), 1)
        self.assertEqual(len(self.deliveries), 2)
        sends_params = [p for m, p in self.c.requests if m == "session/send"]
        self.assertIn("segundo prompt", sends_params[1]["content"])
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)

    async def test_stop_marker_during_warm_exits_clean(self):
        t = threading.Timer(0.3, zc_workers._stop_request, args=(self.T2,))
        t.start()
        self.addCleanup(t.join)
        zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=2),
                                 self.act, "primer prompt")
        self.assertEqual(len(self.deliveries), 1)  # the warm phase ran no turns
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)
        self.assertFalse(self.zt._stop_requested(self.T2))  # marker consumed

    async def test_timeout_releases_everything(self):
        zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=1),
                                 self.act, "primer prompt")
        self.assertEqual(len(self.deliveries), 1)
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)
        self.assertEqual(self.zt._queue_peek(self.T2), [])

    async def test_busy_mac_requeues_and_exits(self):
        # F17 on the warm path: the Mac took the session while we waited —
        # the item GOES BACK to the queue (nothing is lost) and the worker exits
        busy = {"v": False}

        def running():
            return {self.T2: {}} if busy["v"] else {}

        def _flip():
            busy["v"] = True
            zc_workers._queue_put(self.T2, self.act, "durante warm")

        t = threading.Timer(0.3, _flip)
        t.start()
        self.addCleanup(t.join)
        with mock.patch.object(zc_workers, "running_turns", running):
            zc_workers._queue_put(self.T2, self.act, "durante warm")
            zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=2),
                                     self.act, "primer prompt")
        self.assertEqual(len(self.zt._queue_peek(self.T2)), 1)
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)

    async def test_failed_warm_turn_keeps_waiting(self):
        ends = [{"event": "turn.completed"},
                {"event": "turn.failed"},
                {"event": "turn.completed"}]
        with mock.patch.object(zc_workers, "_turn_end_poll",
                               lambda *a, **k: ends.pop(0)):
            zc_workers._queue_put(self.T2, self.act, "va a fallar")
            zc_workers._queue_put(self.T2, self.act, "and this one works")
            zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=1),
                                     self.act, "primero")
        self.assertTrue(any("failed" in s for s in self.sends))
        self.assertEqual(len(self.deliveries), 2)  # the 1st and the one that worked
        self.assertIsNone(self.zt._workers().get(self.T2))

    async def test_knob_zero_classic_behavior(self):
        # warm_keepalive_s=0: neither pickup nor waiting — the queue stays
        # asleep (the drain spawns nothing: _spawn_worker neutralized) and
        # the runtime dies
        zc_workers._queue_put(self.T2, self.act, "stays asleep")
        with mock.patch.object(zc_workers, "_spawn_worker",
                               return_value=None):
            zc_workers.prompt_worker(_cfg(self.zt), self.act, "unico")
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(len(self.zt._queue_peek(self.T2)), 1)
        self.assertEqual(len(self.makes), 1)
        self.assertTrue(self.c.closed)

    async def test_heartbeat_refreshes_and_marks(self):
        w = self.zt._workers()
        w.register(self.T2, {"t0": time.time(), "pid": 1})
        try:
            self.assertTrue(w.heartbeat(self.T2))
            ent = w.get(self.T2)
            self.assertTrue(ent.get("waiting"))
            self.assertFalse(w.heartbeat("sess_inexistente"))
        finally:
            w.pop(self.T2)

    async def test_foreign_pop_does_not_take_the_live_entry(self):
        # incident 15:40: the DUPLICATED worker's atexit wiped the live
        # warm worker's entry — /stop and the guards went blind
        import os
        w = self.zt._workers()

        def foreign_write(q):  # register overwrites the pid: write the store directly
            # (pid 1 = launchd, alive: if it were dead _prune would purge it)
            q = dict(q) if isinstance(q, dict) else {}
            q[self.T2] = {"t0": time.time(), "pid": 1}
            return q
        w._store.update_cross_process(foreign_write, {})
        self.assertFalse(w.pop_if_mine(self.T2))  # foreign: untouched
        self.assertIsNotNone(w.get(self.T2))
        w.pop(self.T2)
        w.register(self.T2, {"t0": time.time()})
        self.assertTrue(w.pop_if_mine(self.T2))  # own: out
        self.assertIsNone(w.get(self.T2))
        self.assertFalse(w.pop_if_mine("sess_inexistente"))


class TestRequeueFIFOOrder(unittest.IsolatedAsyncioTestCase):
    """R0c — the warm requeue PRESERVES FIFO order. The latent
    regression: _warm_wait re-queued with reversed() + append
    (_queue_put appends), which INVERTED the queue with ≥2 items — the
    user's messages would have run backwards. Proven with 3."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}

    async def test_pickup_takes_first_and_queue_stays_in_order(self):
        for m in ("one", "two", "three"):
            zc_workers._queue_put(self.T2, self.act, m)
        text, act = zc_workers._warm_wait(_cfg(self.zt), self.T2, 5)
        self.assertEqual(text, "one")
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T2)],
                         ["two", "three"])

    async def test_busy_task_returns_everything_in_order(self):
        with mock.patch.object(zc_workers, "running_turns",
                               lambda: {self.T2}):
            for m in ("one", "two", "three"):
                zc_workers._queue_put(self.T2, self.act, m)
            self.assertIsNone(
                zc_workers._warm_wait(_cfg(self.zt), self.T2, 5))
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T2)],
                         ["one", "two", "three"])


class TestRecycledByDeploy(unittest.IsolatedAsyncioTestCase):
    """F48d — the warm worker detects a deploy (package stamp,
    config.code_stamp) and retires IDLE: before, it kept going with the
    old modules in memory until the keepalive expired — 10 min of new
    poller + old worker per deploy (20/10 20:37-20:56, seen in the
    e2e)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        from zcode_bot import config as zc_config
        self.cfgmod = zc_config
        self.logs = []
        support.patch(self, zc_workers, "pwlog",
                        lambda *a: self.logs.append(" ".join(map(str, a))))

    async def test_without_deploy_the_stamp_does_not_change(self):
        self.assertFalse(self.cfgmod.code_changed())

    async def test_idle_with_new_code_exits_without_waiting_for_keepalive(self):
        support.patch(self, self.cfgmod, "code_stamp", lambda: "other")
        self.assertIsNone(zc_workers._warm_wait(_cfg(self.zt), self.T2, 30))
        self.assertTrue(any("new code deployed" in ln
                            for ln in self.logs))
        self.assertFalse(any("keepalive expired" in ln for ln in self.logs))

    async def test_queued_items_served_before_retiring(self):
        support.patch(self, self.cfgmod, "code_stamp", lambda: "other")
        zc_workers._queue_put(self.T2, self.act, "one")
        text, _act = zc_workers._warm_wait(_cfg(self.zt), self.T2, 30)
        self.assertEqual(text, "one")  # nothing left hanging in the queue


if __name__ == "__main__":
    unittest.main()
