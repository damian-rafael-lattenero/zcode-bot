"""F39 — honest /stop: worker phase and the kill's own verification.

Incident 20/10 11:18: /stop 50s after starting a task → the worker was
RESUMING the session (~130s in this conversation) and does not check
the marker until it finishes → 4s grace expired → kill -9 → the '💀 no
frenó en 4s (el reporte llega por el chequeo de huérfanos)' banner…
and the report never arrived: the worker was not yet in bot-active,
the check does not see it.
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import workers as zc_workers  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.kills = []
        # the fake pid is "ours" and alive; the kill gets recorded
        support.patch(self, zc_workers, "_run_turn_ok", lambda pid: True)
        support.patch(self, zc_workers, "_proc_alive", lambda pid: True)
        support.patch(self, zc_workers, "_kill_runtime",
                        lambda pid: (self.kills.append(pid), True)[1])
        support.patch(self, zc_workers, "_STOP_VERIFY_S", 0.05)

    def _worker(self, age_s=10):
        self.zt._workers().register(support.T1, {
            "title": "testing", "pid": support.FAKE_PID,
            "t0": time.time() - age_s})
        # the registry stamps the pid of the registering process (this test)
        self.pid = self.zt._workers().get(support.T1).get("pid")


class TestStopDuringStartup(_Base):
    def test_starting_up_not_killed_and_marker_stays(self):
        self._worker(age_s=50)  # like the incident: 50s, resuming
        resp = self.zt.bot_stop(support.T1)
        self.assertIn("Stopping", resp)
        self.assertIn("the runtime is starting", resp)
        self.assertIn("without running your prompt", resp)
        self.assertNotIn("kill -9", resp)
        self.assertEqual(self.kills, [])
        # the marker stays: the worker sees it once it finishes starting
        # up and aborts on its own ('frenado por /stop (antes de enviar)')
        self.assertTrue(self.zt._stop_requested(support.T1))

    def test_hung_startup_killed_anyway(self):
        self._worker(age_s=zc_workers._STOP_STARTUP_MAX_S + 60)
        support.patch(self, zc_workers, "_proc_dead", lambda pid: True)
        resp = self.zt.bot_stop(support.T1, grace=0.05)
        self.assertEqual(self.kills, [self.pid])
        self.assertIn("kill -9", resp)
        self.assertIn("✅ verified: process dead", resp)
        self.assertFalse(self.zt._stop_requested(support.T1))  # consumed


class TestStopDuringTurn(_Base):
    def setUp(self):
        super().setUp()
        self._worker(age_s=300)
        self.zt._active_turn_register(support.T1, "testing",
                                      time.time() - 300)

    def test_kill_verified(self):
        support.patch(self, zc_workers, "_proc_dead", lambda pid: True)
        resp = self.zt.bot_stop(support.T1, grace=0.05)
        self.assertEqual(self.kills, [self.pid])
        self.assertIn("did not stop in 0s: kill -9", resp)
        self.assertIn("✅ verified: process dead", resp)
        # in a turn there IS a possible report: it is in bot-active
        self.assertIn("orphan check", resp)
        self.assertNotIn("(el reporte llega", resp)  # the old banner
        self.assertFalse(self.zt._stop_requested(support.T1))

    def test_kill_but_still_alive_is_reported(self):
        support.patch(self, zc_workers, "_proc_dead", lambda pid: False)
        resp = self.zt.bot_stop(support.T1, grace=0.05)
        self.assertIn("⚠️ the process is still alive", resp)
        self.assertIn("/status", resp)

    def test_stop_marked_in_the_registered_turn(self):
        # F47c: the turn in bot-active gets marked stop=<epoch> — it is
        # what the orphan supervisor reads to say «stopped» instead of
        # «failed» when kill -9 takes the worker down without its finally
        support.patch(self, zc_workers, "_proc_dead", lambda pid: True)
        before = time.time()
        self.zt.bot_stop(support.T1, grace=0.05)
        ent = self.zt._store("bot-active.json").read()[support.T1]
        self.assertGreaterEqual(ent["stop"], before)
        self.assertEqual(ent["title"], "testing")  # the rest, untouched


class TestProcDead(unittest.TestCase):
    """os.kill(pid, 0) counts a ZOMBIE as alive: for /stop it is already dead."""

    def _ps(self, stat):
        def run(args, **kw):
            return subprocess.CompletedProcess(args, 0, stdout=stat + "\n",
                                               stderr="")
        return run

    def test_missing_pid(self):
        with mock.patch.object(zc_workers, "_proc_alive", lambda pid: False):
            self.assertTrue(zc_workers._proc_dead(support.FAKE_PID))

    def test_zombie_counts_as_dead(self):
        with mock.patch.object(zc_workers, "_proc_alive", lambda pid: True), \
                mock.patch.object(zc_workers.subprocess, "run", self._ps("Z+")):
            self.assertTrue(zc_workers._proc_dead(support.FAKE_PID))

    def test_really_alive(self):
        with mock.patch.object(zc_workers, "_proc_alive", lambda pid: True), \
                mock.patch.object(zc_workers.subprocess, "run", self._ps("S+")):
            self.assertFalse(zc_workers._proc_dead(support.FAKE_PID))


if __name__ == "__main__":
    unittest.main()
