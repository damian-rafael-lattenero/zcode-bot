"""F79a — nothing dies silently anymore.

The incident that owns this file: 27/10 14:25:48, a worker/watcher died
mid-turn and left NO trace anywhere (frozen prompt-worker.log, growing
CoT cache, zero tracebacks). The four primitives under test:
- log_exc: the wall engine — msg + the FULL traceback, never truncated
  (the old crash walls amputated the frames with [-500:]).
- hb: the per-cycle beat — an idle loop and a dead one distinguishable
  by mtime alone.
- _install_death_diagnostics: threading.excepthook (a thread dies with
  its full stack in errors.log while the process lives on) +
  faulthandler at every entrypoint.
- the poller beats every cycle (transport liveness for the F79c healer).
"""
import asyncio
import os
import threading
import time
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot import config
from zcode_bot.utils.logging import hb, install_death_diagnostics, log_exc
from zcode_bot.poller import AsyncPoller


def _errors_log():
    with open(os.path.join(config.LOG_DIR, "errors.log")) as f:
        return f.read()


class TestLogExc(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _boom(self):
        raise RuntimeError("boom-f79")

    def test_wall_logs_the_full_traceback_with_the_message(self):
        try:
            self._boom()
        except RuntimeError:
            log_exc("errors", "the wall fired")
        txt = _errors_log()
        self.assertIn("the wall fired", txt)
        self.assertIn("Traceback (most recent call last)", txt)
        self.assertIn("RuntimeError: boom-f79", txt)
        self.assertIn("_boom", txt)  # the frame that named it

    def test_without_active_exception_it_still_logs_the_message(self):
        log_exc("errors", "no active exception")
        self.assertIn("no active exception", _errors_log())


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_hb_creates_and_freshens_the_beat_file(self):
        hb("transport")
        p = os.path.join(config.STATE_DIR, "hb-transport")
        self.assertTrue(os.path.exists(p))
        self.assertLess(abs(time.time() - os.path.getmtime(p)), 5)

    def test_hb_is_never_fatal(self):
        # a heartbeat failure must not kill the loop it measures: a
        # read-only STATE_DIR degrades to a no-op
        real_state = config.STATE_DIR
        config.STATE_DIR = os.path.join(real_state, "no", "such", "dir")
        try:
            hb("transport")  # must not raise
        finally:
            config.STATE_DIR = real_state


class TestDeathDiagnostics(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_a_dead_thread_leaves_its_stack_and_the_process_lives(self):
        install_death_diagnostics()
        old_hook = threading.excepthook
        try:
            def _dies():
                raise RuntimeError("thread-death-f79")

            t = threading.Thread(target=_dies, name="f79-worker")
            t.start()
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        finally:
            threading.excepthook = old_hook
        txt = _errors_log()
        self.assertIn("THREAD DIED", txt)
        self.assertIn("f79-worker", txt)
        self.assertIn("RuntimeError: thread-death-f79", txt)
        self.assertIn("_dies", txt)


class TestPollerBeat(unittest.TestCase):
    """The transport beats hb EVERY cycle (even idle) — the F79c healer
    reads these mtimes; a poller that only beat when busy would look
    dead exactly when it matters."""

    class _Deps:
        def __init__(self):
            self.reload_checks = 0

        def load_config(self):
            return {"enabled": True}

        def configured(self, cfg):
            return True

        def busy_fresh(self):
            return True  # tg busy: idle cycle, no fetch at all

        def orphans_check(self, cfg):
            pass

        def register_menu(self, cfg):
            return True

        def outbox_flush(self, cfg):
            pass

        def reload_pending(self):
            self.reload_checks += 1
            return self.reload_checks > 3

        def clear_reload(self):
            pass

        def log(self, kind, msg):
            pass

        def current_offset(self):
            return 0

        def save_offset(self, o):
            pass

        def fetch(self, cfg, offset, timeout):
            return None, [], 0

        def process(self, cfg, msg):
            pass

    def test_idle_cycles_beat(self):
        d = self._Deps()
        beats = []
        p = AsyncPoller(
            load_config=d.load_config, configured=d.configured,
            fetch=d.fetch, current_offset=d.current_offset,
            save_offset=d.save_offset, process=d.process,
            busy_fresh=d.busy_fresh, orphans_check=d.orphans_check,
            register_menu=d.register_menu, outbox_flush=d.outbox_flush,
            reload_pending=d.reload_pending, clear_reload=d.clear_reload,
            log=d.log, hb_fn=lambda: beats.append(1))
        asyncio.run(asyncio.wait_for(p.run_forever(), timeout=5))
        self.assertEqual(p.cycles, 3)
        self.assertEqual(len(beats), 3)  # one per cycle, idle included


if __name__ == "__main__":
    unittest.main()
