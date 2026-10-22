# -*- coding: utf-8 -*-
"""F58 — the ghost busy and the honest stop.

22/10 11:13-11:19: a turn killed by FORCE (/stop's kill -9 escalation)
never writes turn.completed/failed to the CLI log — which running_turns()
replays — so the session stayed «busy» for 2 h with nothing owning it:
every new message queued into a black hole, /stop answered «no longer
exists» (its only lever, the workers registry, was already empty), six
stops in 70 s, and a /nuclear. The tombstone (bot-forcestop.json) is the
record every force-death leaves behind, and /stop on a ghost clears it
and gives the queued messages back their runner."""
import time
import unittest
from unittest import mock

import support

from zcode_bot import config as zc_config
from zcode_bot import core as zc_core
from zcode_bot import workers as zc_workers


def _forced(zt):
    return zt._store(zc_config.FORCE_STOP_FILE).read() or {}


class TestTombstone(unittest.IsolatedAsyncioTestCase):
    """running_turns() releases a session whose turn died by force — but
    only when the tombstone is NEWER than its turn.started (a real turn
    started after a force-stop is busy again, not masked)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1  # turn.started ~60 s ago, never ended

    def test_the_fixture_session_is_busy_before_any_tombstone(self):
        self.assertIn(self.T1, self.zt.running_turns())

    def test_a_force_stop_releases_the_busy_state(self):
        zc_core.force_turn_stopped(self.T1)
        self.assertNotIn(self.T1, self.zt.running_turns())
        self.assertIn(self.T1, _forced(self.zt))

    def test_an_older_tombstone_releases_nothing(self):
        zc_core.force_turn_stopped(self.T1, ts=time.time() - 120)
        # the fixture's turn.started is ~60 s old: NEWER than the tombstone
        self.assertIn(self.T1, self.zt.running_turns())

    def test_a_turn_started_after_the_tombstone_is_busy_again(self):
        evs = [(200.0, "sess_x", "turn.started", {})]

        class _St:
            def read(self):
                return {"sess_x": 150.0}  # force-stop BEFORE the start

        with mock.patch.object(zc_core, "zc_logs") as logs, \
                mock.patch.object(zc_core, "_store", lambda name: _St()):
            logs.iter_events.return_value = evs
            self.assertEqual(zc_core.running_turns(), {"sess_x": 200.0})


class TestStopOnAGhost(unittest.IsolatedAsyncioTestCase):
    """/stop against the ghost — the exact 11:17 sequence: busy in the
    CLI log, no worker, messages rotting in the queue."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1
        self.act = {"sid": self.T1, "title": "testing",
                    "ws": support.PROJ_BOT}

    def test_it_clears_the_ghost_and_relaunches_the_queue(self):
        self.zt._queue_put(self.T1, self.act, "the new direction")
        self.assertIn(self.T1, self.zt.running_turns())  # the ghost
        spawns = []
        with mock.patch.object(
                zc_workers, "_spawn_worker",
                side_effect=lambda cfg, act, text:
                (spawns.append(text), True)[1]):
            r = zc_workers.bot_stop(self.T1, cfg={"token": "T"})
        self.assertIn("ghost cleared", r)
        self.assertIn("relaunched", r)
        self.assertEqual(spawns, ["the new direction"])
        self.assertNotIn(self.T1, self.zt.running_turns())  # tombstoned
        self.assertEqual(self.zt._queue_peek(self.T1), [])   # consumed

    def test_only_the_first_is_relaunched_the_rest_requeue(self):
        for msg in ("first", "second"):
            self.zt._queue_put(self.T1, self.act, msg)
        spawns = []
        with mock.patch.object(
                zc_workers, "_spawn_worker",
                side_effect=lambda cfg, act, text:
                (spawns.append(text), True)[1]):
            zc_workers.bot_stop(self.T1, cfg={"token": "T"})
        self.assertEqual(spawns, ["first"])
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["second"])

    def test_without_cfg_the_messages_stay_queued_not_lost(self):
        self.zt._queue_put(self.T1, self.act, "one")
        r = zc_workers.bot_stop(self.T1)
        self.assertIn("ghost cleared", r)
        self.assertIn("stay queued", r)
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["one"])
        self.assertNotIn(self.T1, self.zt.running_turns())

    def test_a_free_session_without_run_still_says_so(self):
        # NOT busy in the CLI log and no worker: the classic answer stands
        self.assertIn("no longer exists",
                      zc_workers.bot_stop("sess_NADA"))


class TestEscalationTombstones(unittest.IsolatedAsyncioTestCase):
    """The kill -9 escalation is WHERE ghosts are born — now the
    tombstone is written at birth (22/10 11:13:31: the kill landed, the
    CLI end never did)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.S = support.T1
        self.kills = []

    def test_kill_9_writes_the_tombstone(self):
        from test_stop_brusco import _FakeReg
        reg = _FakeReg({self.S: {"t0": time.time(), "pid": 777,
                                 "title": "CV"}})
        # the escalation only fires for a LIVE turn (bot-active says so)
        self.zt._store("bot-active.json").write(
            {self.S: {"t0": time.time()}})
        for mod, name, val in (
                (zc_workers, "_workers", lambda: reg),
                (zc_workers, "_run_turn_ok", lambda pid: True),
                (zc_workers, "_proc_alive", lambda pid: True),
                (zc_workers, "_kill_runtime",
                 lambda pid: (self.kills.append(pid), True)[1]),
                (zc_workers.time, "sleep", lambda s: None),
        ):
            p = mock.patch.object(mod, name, val)
            p.start()
            self.addCleanup(p.stop)
        r = zc_workers.bot_stop(self.S, grace=0.0)
        self.assertIn("💀", r)
        self.assertEqual(self.kills, [777])
        self.assertIn(self.S, _forced(self.zt))
        self.assertNotIn(self.S, self.zt.running_turns())


class TestNuclearTombstones(unittest.IsolatedAsyncioTestCase):
    """Everything nuclear kills, dies without a CLI turn end — the
    tombstone covers phone runs AND Mac UI turns (the 22/10 black hole
    ended with a /nuclear that left no trace in the CLI log either)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_every_stopped_run_is_tombstoned(self):
        from zcode_bot import nuclear as zc_nuclear
        fake_reg = type("R", (), {
            "items": staticmethod(lambda: []),
            "flag_stop": staticmethod(lambda s: None)})()
        with mock.patch.object(
                zc_nuclear, "_nuclear_snapshot",
                return_value=([("sess_w", {"t0": 1})], {"sess_m": 2}, 0)), \
            mock.patch.object(zc_nuclear, "_workers", lambda: fake_reg), \
            mock.patch.object(zc_nuclear, "_nuc_kill_own_runtimes",
                              lambda: None), \
            mock.patch.object(zc_nuclear, "app_restart_cycle",
                              lambda: {"app_ok": True, "notes": []}), \
            mock.patch.object(zc_nuclear, "_spawn_nuclear_watchdog",
                              lambda: None), \
            mock.patch.object(zc_nuclear.uhttp, "send",
                              lambda *a, **k: True), \
            mock.patch.object(zc_nuclear.time, "sleep",
                              lambda s: None), \
            mock.patch.object(zc_nuclear.subprocess, "run",
                              lambda *a, **k: None):
            zc_nuclear._nuclear_body({"token": "T"})
        forced = _forced(self.zt)
        self.assertIn("sess_w", forced)
        self.assertIn("sess_m", forced)


if __name__ == "__main__":
    unittest.main()
