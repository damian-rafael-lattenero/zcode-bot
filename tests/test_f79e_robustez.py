"""F79e — the ONE retry policy + the real breaker.

The inline (0,1,3) loop that lived inside send, the 429 override and
the implicit next-cycle retry of fetch now share ONE policy
(utils/retry.py) under ONE shared breaker (SyncCircuitBreaker): the
breaker DECIDES (OPEN rejects before any network — no hammering while
Telegram is down, getUpdates is the cheapest HALF_OPEN probe), the
retry EXECUTES (429 waits its REAL retry_after; 4xx fails fast).
The plan's promise, quoted: «cuando Telegram/network está caído, el  # noqa: spanish — Damian's plan, quoted (F79)
bot NO martilla»."""  # noqa: spanish — Damian's plan, quoted (F79)
import time
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot.telegram.circuit_breaker import (
    CircuitBreakerConfig, CircuitBreakerOpen, SyncCircuitBreaker)
from zcode_bot.utils import http as zc_http
from zcode_bot.utils.retry import retry_call


class TestRetryPolicy(unittest.TestCase):
    """retry_call — the ONE body, injectable clock, deterministic."""

    def test_success_first_try_sleeps_nothing(self):
        slept = []
        r = retry_call(lambda: 42, sleep=slept.append)
        self.assertEqual(r, 42)
        self.assertEqual(slept, [])

    def test_backoffs_between_attempts_then_success(self):
        n = []
        def flaky():
            n.append(1)
            if len(n) < 3:
                raise OSError("reset")
            return "ok"
        slept = []
        r = retry_call(flaky, backoffs=(0, 1, 3), sleep=slept.append)
        self.assertEqual(r, "ok")
        self.assertEqual(slept, [1.0, 3.0])

    def test_retry_after_overrides_the_next_backoff(self):
        calls = []
        class E(Exception):
            pass
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise E("429")
            return True
        slept = []
        r = retry_call(flaky, backoffs=(0, 1, 3), sleep=slept.append,
                       retry_after_of=lambda e: 7.0)
        self.assertTrue(r)
        self.assertEqual(slept, [7.5])   # ra + 0.5, not the network's 1

    def test_retry_after_is_capped(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("429")
            return True
        slept = []
        retry_call(flaky, backoffs=(0, 1), sleep=slept.append,
                   retry_after_of=lambda e: 999.0, retry_after_cap=60.0)
        self.assertEqual(slept, [60.5])

    def test_should_retry_false_stops_now(self):
        calls = []
        class Hard(Exception):
            code = 400
        def flaky():
            calls.append(1)
            raise Hard("bad request")
        with self.assertRaises(Hard):
            retry_call(flaky, backoffs=(0, 1, 3), sleep=lambda s: None,
                       should_retry=lambda e: False)
        self.assertEqual(len(calls), 1)   # no hammering with extra steps

    def test_all_attempts_fail_raises_the_last(self):
        class Last(Exception):
            pass
        def always():
            raise Last("down")
        with self.assertRaises(Last):
            retry_call(always, backoffs=(0, 0), sleep=lambda s: None)

    def test_jitter_is_bounded_by_the_injected_rnd(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("x")
            return True
        slept = []
        retry_call(flaky, backoffs=(0, 1), sleep=slept.append,
                   jitter=0.5, rnd=lambda: 1.0)
        self.assertEqual(slept, [1.5])    # exactly backoff + jitter*rnd


class TestSyncBreaker(unittest.TestCase):
    """The state machine for the sync transport, with a movable clock."""

    def _brk(self, **kw):
        t = [1000.0]
        brk = SyncCircuitBreaker("t", CircuitBreakerConfig(
            failure_threshold=3, success_threshold=2, timeout=30.0, **kw),
            time_fn=lambda: t[0])
        return brk, t

    def test_closed_to_open_after_threshold(self):
        brk, _t = self._brk()
        for _ in range(3):
            with self.assertRaises(OSError):
                brk.call(lambda: (_ for _ in ()).throw(OSError("down")))
        self.assertEqual(brk.state, "OPEN")

    def test_open_rejects_fast_without_calling(self):
        brk, _t = self._brk()
        for _ in range(3):
            try:
                brk.call(lambda: (_ for _ in ()).throw(OSError("down")))
            except OSError:
                pass
        called = []
        with self.assertRaises(CircuitBreakerOpen):
            brk.call(lambda: called.append(1) or 1)
        self.assertEqual(called, [])   # rejected BEFORE any work

    def test_timeout_turns_open_into_half_open_probe_then_closes(self):
        brk, t = self._brk()
        for _ in range(3):
            try:
                brk.call(lambda: (_ for _ in ()).throw(OSError("down")))
            except OSError:
                pass
        t[0] += 31.0                     # the timeout elapses
        self.assertEqual(brk.state, "HALF_OPEN")
        self.assertTrue(brk.call(lambda: True))       # the probe
        self.assertEqual(brk.state, "HALF_OPEN")      # 1 of 2
        self.assertTrue(brk.call(lambda: True))
        self.assertEqual(brk.state, "CLOSED")

    def test_half_open_failure_reopens(self):
        brk, t = self._brk()
        for _ in range(3):
            try:
                brk.call(lambda: (_ for _ in ()).throw(OSError("down")))
            except OSError:
                pass
        t[0] += 31.0
        self.assertEqual(brk.state, "HALF_OPEN")
        with self.assertRaises(OSError):
            brk.call(lambda: (_ for _ in ()).throw(OSError("still down")))
        self.assertEqual(brk.state, "OPEN")


class TestSendUnderTheBreaker(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False)
        self.addCleanup(zc_http.BREAKER.reset)
        zc_http.BREAKER.reset()
        self.addCleanup(setattr, zc_http, "_cooldown_until", [0.0])
        zc_http._cooldown_until[0] = 0.0

    def test_open_breaker_fastsends_to_the_outbox_without_network(self):
        brk = zc_http.BREAKER
        import unittest.mock as mock
        with mock.patch.object(zc_http, "tg_api",
                               lambda *a, **k: self.fail("network hit")), \
                mock.patch.object(zc_http, "_sleep", lambda s: None):
            for _ in range(5):   # 5 consecutive failures open it
                self.assertFalse(zc_http.send(self.cfg, "m%d" % _))
            self.assertEqual(brk.state, "OPEN")
            # the 6th: rejected instantly, no tg_api call, to the outbox
            self.assertFalse(zc_http.send(self.cfg, "held"))
        box = self.zt._store("outbox.json").read() or []
        self.assertEqual([b["text"] for b in box][-1], "held")

    def test_4xx_fails_fast_one_attempt_only(self):
        import urllib.error
        import unittest.mock as mock
        calls = []
        def tg_api(cfg, method, params=None, timeout=None):
            calls.append(method)
            raise urllib.error.HTTPError(
                "u", 400, "Bad Request", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(zc_http, "tg_api", tg_api), \
                mock.patch.object(zc_http, "_sleep", lambda s: None):
            self.assertFalse(zc_http.send(self.cfg, "x"))
        self.assertEqual(len(calls), 1)   # no retry on a hard 4xx

    def test_fetch_with_open_breaker_is_an_empty_cycle(self):
        import unittest.mock as mock
        from zcode_bot import updates
        brk = zc_http.BREAKER
        with mock.patch.object(zc_http, "tg_api",
                               lambda *a, **k: self.fail("network hit")):
            try:
                brk.call(lambda: (_ for _ in ()).throw(OSError("down")))
            except OSError:
                pass
        # force it open without burning 5 failures
        brk._state = "OPEN"
        brk._last_failure_time = time.monotonic()
        off, msgs, n = updates.fetch_updates(self.cfg, 7, 3)
        self.assertEqual((off, msgs, n), (7, [], 0))

    def test_health_line_carries_the_breaker_state(self):
        from zcode_bot import healer
        self.assertIn("breaker", healer.health_line())
        self.assertIn("CLOSED", healer.health_line())


if __name__ == "__main__":
    unittest.main()
