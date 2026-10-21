"""F37 — the bot never waits: autonomous queue drain + async commands.

Two halves of the same principle (Damian's directive: "everything
coming in from the phone is async, never wait for what is in progress"):

- F37a queue_check: the dormant F17 queue waited for the on_stop of a
  Mac turn that may NEVER exist (the app swallowed the prompt while the
  task was busy — incident 19/10 15:47, 88 minutes of sleep). Now the
  poller wakes the queues by itself when the session is free.
- F37b handle_command async: slow commands (compact/ctx/usage/
  quota/stop) run in a thread with an instant acknowledgement — the
  poller never blocks and the other messages keep coming in.

House lesson (applied): the queue_check guards live in the SHARED
workers module (support.fresh's zt is a copy of gateway) — patch
zc_workers, not zt. Patching the wrong side executed the REAL
_spawn_worker (the Popen failed contained in the sandbox, no effect,
but that was luck).
"""
import threading
import unittest
from unittest import mock

import support
from zcode_bot.commands import dispatch as zc_dispatch
from zcode_bot.utils import http as zc_http
from zcode_bot import workers as zc_workers


class TestQueueCheck(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        # own throttle: fresh per test (it is shared module state)
        support.patch(self, zc_workers, "_drain_ts", [0.0])
        self.resumes = []
        self.sent = []
        support.patch(self, zc_workers, "_queue_resume",
                        lambda cfg, sid, title=None:
                        (self.resumes.append(sid) or "⏳ Resumed"))
        support.patch(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sent.append((text, loud)), True)[1])
        support.patch(self, zc_workers, "_nuclear_active", lambda: False)
        support.patch(self, zc_workers, "_task_current_ok",
                        lambda cfg, sid: True)
        support.patch(self, zc_workers, "running_turns",
                        lambda max_age=7200: {})
        self.act = {"sid": support.T1, "title": "t", "ws": "/w"}

    def test_wakes_idle_queue_of_free_session(self):
        zc_workers._queue_put(support.T1, self.act, "hello from the queue")
        zc_workers.queue_check({}, every_s=0)
        self.assertEqual(self.resumes, [support.T1])
        self.assertEqual(self.sent, [("⏳ Resumed", False)])

    def test_own_throttle_second_call_does_not_run(self):
        zc_workers._queue_put(support.T1, self.act, "hello")
        zc_workers.queue_check({}, every_s=999)
        zc_workers.queue_check({}, every_s=999)  # inside the window: skip
        self.assertEqual(self.resumes, [support.T1])

    def test_busy_session_sleeps(self):
        """The central guard: if the Mac has a turn in progress, the
        queue waits (its on_stop or the next check will wake it)."""
        zc_workers._queue_put(support.T1, self.act, "hello")
        support.patch(self, zc_workers, "running_turns",
                        lambda max_age=7200: {support.T1: 123.0})
        zc_workers.queue_check({}, every_s=0)
        self.assertEqual(self.resumes, [])

    def test_live_worker_sleeps(self):
        zc_workers._queue_put(support.T1, self.act, "hello")
        support.patch(self, zc_workers, "_workers",
                        lambda: type("R", (), {"get": lambda s, k: {"pid": 1}})())
        zc_workers.queue_check({}, every_s=0)
        self.assertEqual(self.resumes, [])

    def test_nuclear_stops_everything(self):
        zc_workers._queue_put(support.T1, self.act, "hello")
        support.patch(self, zc_workers, "_nuclear_active", lambda: True)
        zc_workers.queue_check({}, every_s=0)
        self.assertEqual(self.resumes, [])

    def test_no_current_task_sleeps(self):
        """F15: the queue of a session that is not the current task waits
        for its /task — the check does NOT wake it on its own."""
        zc_workers._queue_put(support.T1, self.act, "hello")
        support.patch(self, zc_workers, "_task_current_ok",
                        lambda cfg, sid: False)
        zc_workers.queue_check({}, every_s=0)
        self.assertEqual(self.resumes, [])


class TestComandosAsync(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sent = []
        support.patch(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sent.append((text, loud)), True)[1])

    def test_slow_command_runs_in_thread_with_ack(self):
        """The poller receives the ACK as the return value (keeps the
        loop going right away); the report travels later via the thread."""
        sent_ev = threading.Event()

        def send_fake(cfg, text, loud=None, markup=None):
            self.sent.append((text, loud))
            sent_ev.set()
            return True
        support.patch(self, zc_http, "send", send_fake)
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch",
                                  lambda cfg, text: "📋 the /ctx report"):
            r = zc_dispatch.handle_command({}, "/ctx")
        self.assertEqual(r, zc_dispatch.ACK_SLOW)
        self.assertTrue(sent_ev.wait(timeout=5))
        self.assertEqual(self.sent, [("📋 the /ctx report", False)])

    def test_thread_error_reports_does_not_die(self):
        seen = threading.Event()

        def blows_up(cfg, text):
            seen.set()
            raise ValueError("runtime colgado")
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch", blows_up):
            zc_dispatch.handle_command({}, "/ctx")
        sounded = threading.Event()

        def send_fake(cfg, text, loud=None, markup=None):
            self.sent.append((text, loud))
            sounded.set()
            return True
        support.patch(self, zc_http, "send", send_fake)
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch", blows_up):
            zc_dispatch.handle_command({}, "/ctx")
        self.assertTrue(sounded.wait(timeout=5))
        self.assertTrue(any("Error in /ctx" in t and "runtime colgado" in t
                            for t, _l in self.sent))
        self.assertTrue(any(l for _t, l in self.sent))  # the error is LOUD

    def test_fast_command_stays_synchronous(self):
        """Instant commands (task/mode/help/...) do NOT go through a
        thread: the response is still the return value, as always."""
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch",
                                  mock.MagicMock(return_value="🏓 pong")) as d:
            r = zc_dispatch.handle_command({}, "/ping")
        self.assertEqual(r, "🏓 pong")
        self.assertEqual(d.call_count, 1)

    def test_suite_sync_mode(self):
        """support.fresh turns off SLOW_ASYNC: the goldens keep reading
        the handler's return value, no threads."""
        self.assertFalse(zc_dispatch.SLOW_ASYNC)
        with mock.patch.object(zc_dispatch.REG, "dispatch",
                               lambda cfg, text: "directo"):
            self.assertEqual(zc_dispatch.handle_command({}, "/ctx"), "directo")


if __name__ == "__main__":
    unittest.main()
