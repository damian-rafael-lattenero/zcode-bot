"""F83 — the kill loop of 27/10 22:26, closed at the root.

The forensics (healer.log: 7 kickstarts in 7 minutes, exit status -15
every time): a poller cycle spending minutes inside 429 retry sleeps
beat hb only at cycle END, read as dead to the dumb healer (mtime is
its whole world), and each SIGTERM respawned a poller with a FRESH
circuit breaker — arriving to re-heat the very rate limit that stalled
its predecessor. Two roots, two fixes: (D1) hb answers "is the process
alive" — a watchdog thread beats it while the cycle works (poller.hb_watchdog,
5 s in prod); (D2) a 429 parks — send pre-checks the cooldown window
the first 429 set and no longer sleeps retry_after out inside one call
(the live edits already skipped; send was the one burning attempts)."""
import asyncio
import io
import time
import unittest
import urllib.error
import unittest.mock as mock

import support  # src on sys.path (deterministic)

from zcode_bot.poller import AsyncPoller
from zcode_bot.utils import http as zc_http


class _SlowDeps:
    """AsyncPoller deps with a FETCH THAT BLOCKS — one long cycle, the
    exact shape of the incident (the work inside one iteration)."""

    def __init__(self, fetch_s=0.25):
        self.fetch_s = fetch_s
        self.reload_checks = 0

    def load_config(self):
        return {}

    def configured(self, cfg):
        return True    # the fetch path must RUN — it is the slow part

    def fetch(self, cfg, offset, timeout):
        time.sleep(self.fetch_s)
        return None, [], 0

    def current_offset(self):
        return 0

    def save_offset(self, o):
        pass

    def process(self, cfg, msg):
        pass

    def busy_fresh(self):
        return False   # not busy: the fetch happens every cycle

    def orphans_check(self, cfg):
        pass

    def register_menu(self, cfg):
        return True

    def outbox_flush(self, cfg):
        pass

    def reload_pending(self):
        self.reload_checks += 1
        return self.reload_checks > 2   # two slow cycles, then exit

    def clear_reload(self):
        pass

    def log(self, kind, msg):
        pass


class TestTheWatchdogBeat(unittest.TestCase):
    def test_beats_while_one_cycle_works(self):
        d = _SlowDeps(fetch_s=0.30)
        beats = []
        p = AsyncPoller(
            load_config=d.load_config, configured=d.configured,
            fetch=d.fetch, current_offset=d.current_offset,
            save_offset=d.save_offset, process=d.process,
            busy_fresh=d.busy_fresh, orphans_check=d.orphans_check,
            register_menu=d.register_menu, outbox_flush=d.outbox_flush,
            reload_pending=d.reload_pending, clear_reload=d.clear_reload,
            log=d.log, hb_fn=lambda: beats.append(1),
            watchdog_s=0.05)
        asyncio.run(asyncio.wait_for(p.run_forever(), timeout=10))
        self.assertEqual(p.cycles, 2)
        # the contract of D1: MORE beats than cycles — the line kept
        # beating while the two long fetches blocked (cycle-end-only
        # beats are exactly how the healer misread a working poller)
        self.assertGreater(len(beats), p.cycles)
        self.assertIsNotNone(p._watchdog_stop)

    def test_the_thread_stops_with_the_loop(self):
        d = _SlowDeps(fetch_s=0.02)
        p = AsyncPoller(
            load_config=d.load_config, configured=d.configured,
            fetch=d.fetch, current_offset=d.current_offset,
            save_offset=d.save_offset, process=d.process,
            busy_fresh=d.busy_fresh, orphans_check=d.orphans_check,
            register_menu=d.register_menu, outbox_flush=d.outbox_flush,
            reload_pending=d.reload_pending, clear_reload=d.clear_reload,
            log=d.log, hb_fn=lambda: None, watchdog_s=0.05)
        asyncio.run(asyncio.wait_for(p.run_forever(), timeout=10))
        self.assertTrue(p._watchdog_stop.is_set())

    def test_default_off_keeps_the_per_cycle_contract(self):
        d = _SlowDeps(fetch_s=0.0)
        beats = []
        p = AsyncPoller(
            load_config=d.load_config, configured=d.configured,
            fetch=d.fetch, current_offset=d.current_offset,
            save_offset=d.save_offset, process=d.process,
            busy_fresh=d.busy_fresh, orphans_check=d.orphans_check,
            register_menu=d.register_menu, outbox_flush=d.outbox_flush,
            reload_pending=d.reload_pending, clear_reload=d.clear_reload,
            log=d.log, hb_fn=lambda: beats.append(1))
        self.assertIsNone(p._watchdog_s)   # no watchdog wired
        asyncio.run(asyncio.wait_for(p.run_forever(), timeout=10))
        self.assertEqual(len(beats), p.cycles)  # exactly per cycle


class TestSendParks(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False)
        self.addCleanup(zc_http.BREAKER.reset)
        zc_http.BREAKER.reset()
        self.addCleanup(setattr, zc_http, "_cooldown_until", [0.0])
        zc_http._cooldown_until[0] = 0.0

    def test_cooldown_window_parks_without_network(self):
        zc_http._cooldown_until[0] = time.time() + 30
        with mock.patch.object(zc_http, "tg_api",
                               lambda *a, **k: self.fail("network hit")), \
                mock.patch.object(zc_http, "_sleep",
                                  lambda s: self.fail("slept")):
            self.assertFalse(zc_http.send(self.cfg, "parked"))
        box = self.zt._store("outbox.json").read() or []
        self.assertEqual([b["text"] for b in box][-1], "parked")

    def test_cooldown_window_without_outbox_just_refuses(self):
        zc_http._cooldown_until[0] = time.time() + 30
        with mock.patch.object(zc_http, "tg_api",
                               lambda *a, **k: self.fail("network hit")):
            self.assertFalse(zc_http.send(self.cfg, "x", a_outbox=False))

    def test_first_429_parks_sets_the_window_no_sleep(self):
        calls = []
        body = io.BytesIO(b'{"parameters": {"retry_after": 7}}')

        def tg_api(cfg, method, params=None, timeout=None):
            calls.append(method)
            raise urllib.error.HTTPError(
                "u", 429, "Too Many Requests",
                {"content-type": "application/json"}, body)

        sleeps = []
        before = time.time()
        with mock.patch.object(zc_http, "tg_api", tg_api), \
                mock.patch.object(zc_http, "_sleep", sleeps.append):
            self.assertFalse(zc_http.send(self.cfg, "held"))
        # ONE attempt, ZERO sleeps: the server named the window — the
        # old body slept 7.5 s inside this call and retried into the
        # same hot bucket
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        # the body is consumed by the should_retry read; the except's
        # re-read defaults to 5 — what matters is a window got SET
        self.assertGreater(zc_http._cooldown_until[0], before)
        box = self.zt._store("outbox.json").read() or []
        self.assertEqual([b["text"] for b in box][-1], "held")

    def test_the_window_then_parks_every_later_send(self):
        zc_http._cooldown_until[0] = time.time() + 60
        with mock.patch.object(zc_http, "tg_api",
                               lambda *a, **k: self.fail("network hit")):
            for i in range(3):
                self.assertFalse(zc_http.send(self.cfg, "m%d" % i))
        box = self.zt._store("outbox.json").read() or []
        self.assertEqual([b["text"] for b in box][-3:],
                         ["m0", "m1", "m2"])


if __name__ == "__main__":
    unittest.main()
