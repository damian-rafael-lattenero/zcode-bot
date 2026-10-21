"""F17 — explicit queue for "the Mac is busy" (Damian's request
2026-10-13: "queue the message and tell me the Mac is busy and that my
message is in the queue, and /stop should clear the queue").

Context of the incident that motivated the request: with the task busy
from the Mac's run, every phone message spawned a ~500MB runtime just
to discover "busy", queued it, and the worker's drain relaunched it in
a LOOP (5 workers in 5 s in a real pwlog).
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

import support
from zcode_bot import workers as zc_workers        # owning module (F4g)
from zcode_bot.utils import http as zc_http


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1  # active and RUNNING in the fixture
        self.T2 = support.T2  # completed in the fixture
        self.act1 = {"sid": self.T1, "title": "testing", "ws": support.PROJ_BOT}
        self.act2 = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        # F4g: prompt_worker lives in workers and uses uhttp.* — the
        # transport is stubbed at the shared seam (the gateway keeps its
        # own binding for handle_free_text, not yet migrated)
        support.patch(self, self.zt, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), True)[1])
        for name, fn in (("send", lambda cfg, text, loud=None, markup=None:
                          (self.sends.append(text), True)[1]),
                         ("_live_send", lambda cfg, text: 555),
                         ("_live_edit", lambda cfg, mid, text: True)):
            pp = mock.patch.object(zc_http, name, fn)
            pp.start()
            self.addCleanup(pp.stop)
        self.spawns = []

        def spy_spawn(cfg, act, text):
            self.spawns.append((act.get("sid"), text))
            return object()
        # the spy covers ALL spawn consumers: workers
        # (_queue_launch_next/_queue_resume) and commands.dispatch
        # (handle_free_text)
        from zcode_bot.commands import handlers_tasks as zc_tareas
        support.patch(self, self.zt, "_spawn_worker", spy_spawn)
        for mod in (zc_workers, zc_tareas):
            pw = mock.patch.object(mod, "_spawn_worker", spy_spawn)
            pw.start()
            self.addCleanup(pw.stop)


class TestBusyInline(_Base):
    """Free text while the task is busy: queues and NOTIFIES — no spawn."""

    async def test_busy_queues_and_notifies(self):
        zt = self.zt
        self.assertIn(self.T1, zt.running_turns())  # fixture precondition
        resp = zt.handle_free_text(_cfg(zt), "keep fixing the bug")
        self.assertIn("busy", resp)
        self.assertIn("queue", resp)
        self.assertEqual(self.spawns, [])          # NOT one process launched
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)

    async def test_successive_messages_pile_up_and_position(self):
        zt = self.zt
        r1 = zt.handle_free_text(_cfg(zt), "one")
        r2 = zt.handle_free_text(_cfg(zt), "two")
        self.assertIn("1 waiting", r1)
        self.assertIn("2 waiting", r2)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T1)],
                         ["one", "two"])

    async def test_frees_up_if_task_not_running(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)  # T2 completed in the fixture
        resp = zt.handle_free_text(_cfg(zt), "continue with the CV")
        self.assertIn("working", resp)
        self.assertEqual(self.spawns, [(self.T2, "continue with the CV")])

    async def test_worker_also_spawns_no_runtime_when_busy(self):
        # defense in depth: /run or relays arriving directly at the
        # worker also do not open a runtime just to discover "busy"
        zt = self.zt
        support.patch(self, zc_workers, "zc_proto", types.SimpleNamespace(
            make=lambda **kw: (_ for _ in ()).throw(AssertionError(
                "must not spawn a runtime while the task is busy"))))
        zt.prompt_worker(_cfg(zt), self.act1, "message")
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)
        self.assertTrue(any("busy" in s for s in self.sends))
        self.assertIsNone(zt._workers().get(self.T1))  # not left registered


class TestDrainDoesNotLoop(_Base):
    """The queue relay SLEEPS while the session stays busy (regression
    of the 18:03 loop)."""

    async def test_drain_asleep_while_busy(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "one")
        zt._queue_drain(_cfg(zt), self.T1)
        self.assertEqual(self.spawns, [])
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)  # intact

    async def test_drain_relays_if_free_and_current(self):
        zt = self.zt
        zt._queue_put(self.T2, self.act2, "one")
        zt.set_active_bot_session(self.act2)
        zt._queue_drain(_cfg(zt), self.T2)
        self.assertEqual(self.spawns, [(self.T2, "one")])


class TestOnStopWakesQueue(_Base):
    """The Mac finished its turn → the current task's queue runs on its own."""

    def _p(self, sid):
        return {"sessionId": sid, "responsePreview": "done"}

    async def test_resume_when_the_mac_finishes(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "deferred message")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [(self.T1, "deferred message")])
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertTrue(any("Resumed" in s for s in self.sends))

    async def test_with_bot_worker_no_duplicate(self):
        # the worker's drain owns the relay: on_stop does not compete
        zt = self.zt
        zt._workers().register(self.T1, {"title": "testing"})
        zt._queue_put(self.T1, self.act1, "x")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [])
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)  # the drain decides

    async def test_no_current_no_resume(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)
        zt._queue_put(self.T1, self.act1, "x")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [])  # F15: sleeps until /task


class TestStopClearsQueue(_Base):
    """/stop halts AND clears that task's queue."""

    async def test_stop_with_worker_clears(self):
        zt = self.zt
        zt._workers().register(self.T1, {"title": "testing"})
        zt._queue_put(self.T1, self.act1, "one")
        zt._queue_put(self.T1, self.act1, "two")
        resp = zt.bot_stop(self.T1)
        self.assertIn("Stopping", resp)
        self.assertIn("queue cleared (2)", resp)
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertTrue(zt._stop_requested(self.T1))  # actor's marker
        zt._stop_consume(self.T1)

    async def test_stop_without_worker_only_clears(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "one")
        resp = zt.bot_stop(self.T1)
        self.assertIn("Cleared the queue", resp)
        self.assertIn("1", resp)
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertFalse(zt._stop_requested(self.T1))  # nothing to halt

    async def test_stop_with_nothing_as_before(self):
        self.assertIn("no longer exists", self.zt.bot_stop(self.T1))

    async def test_listing_shows_the_queues(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "one")
        rep = zt.stop_list_report()
        text = rep if isinstance(rep, str) else rep[0]
        self.assertIn("Queue", text)
        self.assertIn("1", text)
        markup = rep[1] if isinstance(rep, tuple) else None
        flat = [b["callback_data"] for row in markup["inline_keyboard"]
                for b in row] if markup else []
        self.assertIn("s:%s" % self.T1, flat)  # button that clears


class TestQgoQdel(_Base):
    """/qgo and /qdel against the REAL queue (refactor regression R0a:
    F34b used `config.QUEUE_FILE` in _queue_mutate without importing the
    module — a NameError that left both commands broken since 19/10
    12:55, with the suite green because no test exercised them for
    real)."""

    async def test_qdel_discards_from_the_real_queue(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "one")
        zt._queue_put(self.T1, self.act1, "two")
        resp = zt._cmd_qdel(_cfg(zt), "%s:0" % self.T1)
        self.assertIn("🗑 Discarded: one", resp)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T1)],
                         ["two"])

    async def test_qdel_old_index_discards_nothing(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "one")
        resp = zt._cmd_qdel(_cfg(zt), "%s:5" % self.T1)
        self.assertIn("no longer there", resp)
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)

    async def test_qgo_launches_and_takes_from_the_queue(self):
        # T2 is COMPLETED in the fixture: passes the busy guard
        zt = self.zt
        zt._queue_put(self.T2, self.act2, "one")
        zt._queue_put(self.T2, self.act2, "two")
        resp = zt._cmd_qgo(_cfg(zt), "%s:1" % self.T2)
        self.assertIn("⏫ Launched: two", resp)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T2)],
                         ["one"])
        self.assertEqual(self.spawns[-1], (self.T2, "two"))

    async def test_qgo_with_busy_session_does_not_take_it(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "one")
        resp = zt._cmd_qgo(_cfg(zt), "%s:0" % self.T1)
        self.assertIn("still busy", resp)
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)

    async def test_full_queue_notifies_which_one_dropped(self):
        # F51b: on the 6th enqueue the oldest was dropped IN SILENCE
        zt = self.zt
        sends = []
        with mock.patch.object(zc_http, "send",
                               lambda cfg, text, loud=None, markup=None:
                               sends.append(text) or True):
            cfg = _cfg(zt)
            for i in range(5):
                zt._queue_put(self.T1, self.act1, "msg %d" % i, cfg=cfg)
            self.assertEqual(sends, [])  # up to here everything fits
            zt._queue_put(self.T1, self.act1, "the sixth one", cfg=cfg)
        self.assertEqual(len(sends), 1)
        self.assertIn("Queue full (5)", sends[0])
        self.assertIn("msg 0", sends[0])
        # the LAST 5 remain (FIFO: the oldest was the one dropped)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T1)],
                         ["msg 1", "msg 2", "msg 3", "msg 4", "the sixth one"])

    async def test_full_queue_without_cfg_does_not_try_to_notify(self):
        # internal re-enqueues (requeue) have no cfg: no notice, no
        # blow-up — the drop is only recorded in the future pwlog
        zt = self.zt
        for i in range(6):
            self.assertTrue(zt._queue_put(self.T1, self.act1, "m%d" % i))
        self.assertEqual(len(zt._queue_peek(self.T1)), 5)


if __name__ == "__main__":
    unittest.main()
