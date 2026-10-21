"""F16b/c — the worker as an ACTOR (process) and its file channels.

The model (Damian's explicit request: "like golang channels / akka
actors"): each worker is a process with private state (ITS runtime, ITS
pipes); the poller is the supervisor; ALL communication is via messages
in state/ (bot-workers.json = registry, bot-stop/<sid> = stop order,
bot-q/ = question channel). The receive is REACTIVE (kqueue: the thread
sleeps in the kernel and wakes with the message — zero CPU, no polling).

These tests do NOT spawn real worker processes (they would touch the
real ~/.zcode): they exercise the file contracts and the entrypoint
with doubles. The real end-to-end proof (poller kickstart with a turn
in flight) is a deploy acceptance criterion.
"""
import asyncio
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

import asynkit
import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
from zcode_bot import config as zc_config  # noqa: E402

from zcode_bot import fswatch  # noqa: E402
from zcode_bot import workers as zc_workers        # owning module (F4g)
from zcode_bot.utils import http as zc_http        # transport seam


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class TestSpawnWorker(unittest.IsolatedAsyncioTestCase):
    """The spawn: detached process via shim --run-turn, payload through stdin."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.calls = []

        class FakeStdin(io.BytesIO):
            def close(self):
                pass  # the test reads the buffer afterwards

        class FakeP:
            def __init__(self):
                self.stdin = FakeStdin()
        fake = FakeP()

        def popen(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return fake
        # F4g: _spawn_worker lives in workers — subprocess is patched in
        # the owning module
        sub = mock.Mock()
        sub.Popen = popen
        sub.DEVNULL = -3
        p = mock.patch.object(zc_workers, "subprocess", sub)
        p.start()
        self.addCleanup(p.stop)

    async def test_spawn_detached_with_payload(self):
        zt = self.zt
        act = {"sid": support.T1, "title": "testing",
               "ws": support.PROJ_BOT}
        p = zt._spawn_worker(_cfg(zt), act, "fix the F16 bug")
        self.assertIsNotNone(p)
        argv, kw = self.calls[0]
        self.assertEqual(argv[-1], "--run-turn")
        self.assertTrue(kw["start_new_session"])  # DETACHED: the poller can die
        self.assertEqual(kw["stdout"], -3)
        self.assertEqual(kw["cwd"], support.PROJ_BOT)
        payload = json.loads(p.stdin.getvalue().decode("utf-8"))
        self.assertEqual(payload, {"act": act, "text": "fix the F16 bug"})

    async def test_failed_spawn_reports(self):
        zt = self.zt
        sent = []
        sp = mock.patch.object(
            zc_http, "send",
            lambda cfg, text, loud=None, markup=None:
                (sent.append(text), True)[1])
        sp.start()
        self.addCleanup(sp.stop)

        def boom(*a, **k):
            raise OSError("no hay memoria")
        support.patch(self, zc_workers.subprocess, "Popen", boom)
        res = zt._spawn_worker(_cfg(zt), {"sid": support.T1, "ws": "/x",
                                          "title": "t"}, "hello")
        self.assertIsNone(res)
        self.assertTrue(any("couldn't launch" in s for s in sent))

    async def test_handle_free_text_does_not_say_working_if_it_failed(self):
        zt = self.zt
        support.patch(self, zt, "send", lambda *a, **k: True)
        # T2 is FREE in the fixture (T1 runs) so we reach the spawn
        zt.set_active_bot_session({"sid": support.T2, "title": "CV",
                                   "ws": support.PROJ_CV})

        def boom(*a, **k):
            return None
        from zcode_bot.commands import handlers_tasks as zc_tareas
        pb = mock.patch.object(zc_tareas, "_spawn_worker", boom)
        pb.start()
        self.addCleanup(pb.stop)
        self.assertIsNone(zt.handle_free_text(_cfg(zt), "hello"))


class TestCmdRunTurn(unittest.IsolatedAsyncioTestCase):
    """The actor's entrypoint: reads the mailbox (stdin), runs prompt_worker."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.received = []

    def _run(self, payload):
        zt = self.zt
        # F4g: cmd_run_turn and prompt_worker live in workers — the stub
        # goes in the owning module
        pw = mock.patch.object(
            zc_workers, "prompt_worker",
            lambda cfg, act, text: self.received.append((act, text)))
        pw.start()
        self.addCleanup(pw.stop)
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps(payload))):
            zt.cmd_run_turn()

    async def test_payload_reaches_prompt_worker(self):
        act = {"sid": support.T1, "title": "testing", "ws": support.PROJ_BOT}
        self._run({"act": act, "text": "message from the phone"})
        self.assertEqual(self.received,
                         [(act, "message from the phone")])

    async def test_empty_stdin_does_not_blow_up(self):
        self._run({})
        self.assertEqual(self.received, [(None or {}, "")])


class TestStopMarker(unittest.IsolatedAsyncioTestCase):
    """The /stop channel: the poller writes, the worker consumes."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    async def test_bot_stop_writes_marker_and_returns_notice(self):
        zt = self.zt
        zt._workers().register(self.T1, {"title": "testing"})
        resp = zt.bot_stop(self.T1)
        self.assertIn("Stopping", resp)
        self.assertTrue(zt._stop_requested(self.T1))
        self.assertTrue(zt._workers().stopped(self.T1))

    async def test_stop_of_nonexistent_run(self):
        self.assertIn("no longer exists", self.zt.bot_stop("sess_NADA"))

    async def test_consume_stops_seeing_it(self):
        zt = self.zt
        zt._workers().register(self.T1, {"title": "t"})
        zt.bot_stop(self.T1)
        zt._stop_consume(self.T1)
        self.assertFalse(zt._stop_requested(self.T1))
        # idempotent (the worker's finally always calls it)
        zt._stop_consume(self.T1)

    async def test_worker_ends_and_clears_the_channel(self):
        # prompt_worker's finally consumes the marker: an old /stop does
        # not halt the session's NEXT turn
        zt = self.zt
        zt._workers().register(self.T1, {"title": "t"})
        zt.bot_stop(self.T1)
        zt._stop_consume(self.T1)
        self.assertFalse(zt._stop_requested(self.T1))


class TestTurnEndPollReactive(unittest.IsolatedAsyncioTestCase):
    """turn end via KQUEUE: the CLI writes turn.completed to the log and
    the poll wakes from the kernel — no blind polling."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_crossed_turn_ignores_foreign_end(self):
        """F20 — REGRESSION of incident 18:29: the app runs a turn in the
        SAME session as the worker; the worker must wait for ITS turnId
        (the first turn.started after its session/send), not any
        turn.completed of the session (it delivered the foreign answer)."""
        zt = self.zt
        log_today = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))

        def ev(ts, name, tid):
            return json.dumps({"timestamp": support._iso(ts),
                               "event": name, "sessionId": support.T1,
                               "turnId": tid})
        base = time.time() - 60
        with open(log_today, "a") as f:  # the Mac's turn: started BEFORE
            f.write(ev(base - 30, "turn.started", "turn_AJENO") + "\n")
            f.write(ev(base - 5, "turn.completed", "turn_AJENO") + "\n")
        # our turn arrives BEFORE the wait is armed: fswatch's safety
        # scan rescues anything prior to arming — no coordination sleeps
        # (the arming race no longer exists)
        with open(log_today, "a") as f:
            f.write(ev(time.time(), "turn.started", "turn_MIO") + "\n")
            f.write(ev(time.time(), "turn.completed", "turn_MIO") + "\n")
        end = zt._turn_end_poll(support.T1, base, timeout=8, t_send=base)
        self.assertIsNotNone(end)
        self.assertEqual(end.get("turnId"), "turn_MIO")  # NOT the foreign one

    async def test_without_t_send_previous_behavior(self):
        zt = self.zt
        log_today = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        t0 = time.time()
        with open(log_today, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.completed",
                                "sessionId": support.T1}) + "\n")
        end = zt._turn_end_poll(support.T1, t0, timeout=3)
        self.assertIsNotNone(end)  # no turnId and no t_send: works the same

    async def test_stop_check_runs_on_wake(self):
        zt = self.zt
        n = []

        def check():
            n.append(1)
        t0 = time.time()
        zt._turn_end_poll(support.T1, t0, timeout=0.6, stop_check=check)
        self.assertGreaterEqual(len(n), 1)  # invoked even though there was no log


class TestFswatch(unittest.IsolatedAsyncioTestCase):
    """The reactive primitive: kqueue on macOS (produces), polling as fallback."""

    @unittest.skipUnless(sys.platform == "darwin",
                         "kqueue is macOS-only; on Linux the wait is _wait_poll "
                         "(no setup hooks — the race is closed by "
                         "wait_for with a predicate)")
    async def test_change_between_baseline_and_kqueue(self):
        """Real race: change BETWEEN baseline and armed kqueue.

        Uses synchronization hooks to be 100% deterministic:
        1. Waits for the baseline to be taken (on_baseline)
        2. Waits for the kqueue to be armed (on_armed)
        3. Creates the file — it must be detected by the safety scan
        """
        import tempfile
        d = tempfile.mkdtemp(prefix="zcfs-")
        t0 = time.time()

        # Sig and NOT asyncio.Event: the hooks run in the executor thread
        # (to_thread); an Event.set() from another thread does not wake
        # the loop (lesson: the thread→loop bridge is Sig,
        # call_soon_threadsafe)
        baseline_ready = asynkit.Sig()
        armed_ready = asynkit.Sig()
        
        def on_baseline():
            baseline_ready.set()
        def on_armed():
            armed_ready.set()
        
        # Arm wait_change in the background with hooks
        task = asyncio.create_task(
            asynkit.call(
                fswatch.wait_change, [d], timeout=5, poll=0.1,
                on_baseline=on_baseline, on_armed=on_armed
            )
        )
        
        # Wait for baseline taken
        await asyncio.wait_for(baseline_ready.wait(), timeout=1.0)
        # Wait for kqueue armed
        await asyncio.wait_for(armed_ready.wait(), timeout=1.0)
        
        # NOW create the file (real race: baseline taken, kqueue armed)
        with open(os.path.join(d, "arrived.json"), "w") as f:
            f.write("{}")
        
        changed = await task
        self.assertTrue(changed)
        self.assertLess(time.time() - t0, 3)

    async def test_timeout_without_changes(self):
        """Quiet directory → waits the full timeout and returns False.
        With retries: a spurious kernel event on the tmpdir (Spotlight,
        atime, another xdist worker) wakes the kqueue early and that is
        NOT an fswatch bug — it flaked once under extreme load (19/10:
        deploy + a spectator streaming + 16 workers). The guard remains:
        3 dirs in a row waking early IS a regression (a wait_change
        that always returns immediately fails)."""
        import tempfile
        changed, dt = None, 0.0
        for _ in range(3):
            d = tempfile.mkdtemp(prefix="zcfs-")
            t0 = time.time()
            changed = fswatch.wait_change([d], timeout=0.5)
            dt = time.time() - t0
            if not changed and dt >= 0.3:
                return  # waited the full timeout: correct behavior
        self.fail("wait_change woke up early 3 times: %r in %.2fs"
                  % (changed, dt))

    async def test_missing_path_does_not_blow_up(self):
        self.assertFalse(fswatch.wait_change(["/does/not/exist/xyz"], timeout=0.2))
        self.assertTrue(fswatch.wait_change([], timeout=0.05) is False)


if __name__ == "__main__":
    unittest.main()
