"""F11 — measure and dampen the poller's RSS peak during live mode.

Finding from the "Testing 123" turn (2026-10-13 15:44): a newborn poller
(rss=20MB) peaked at 3561MB in 34 s with a session/events frame of ONLY
15MB. ru_maxrss (historical peak) can't tell transient from retained:
1. _rss_now_mb(): CURRENT RSS via ps (the poller does NOT run under
   sandbox — only its runtimes do — so ps works for it).
2. _poll_cadence(): the protocol REJECTS afterEventId (verified live:
   ZodError unrecognized_keys) → without incremental, the lever is the
   frequency: frame >2MB → poll every 5 s instead of 2 s.
"""
import os
import unittest

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


class TestPollCadence(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_small_frame_normal_cadence(self):
        self.assertEqual(self.zt._poll_cadence(64 * 1024), 2.0)
        self.assertEqual(self.zt._poll_cadence(0), 2.0)

    async def test_large_frame_slow_cadence(self):
        self.assertEqual(self.zt._poll_cadence(15 * 1024 * 1024), 5.0)
        justo = self.zt.LIVE_BIG_FRAME
        self.assertEqual(self.zt._poll_cadence(justo), 2.0)   # threshold is exclusive
        self.assertEqual(self.zt._poll_cadence(justo + 1), 5.0)


class TestRssNow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_parses_ps_output(self):
        class FakeRun:
            def __call__(self, *a, **kw):
                class R:
                    stdout = "  3145728 \n"  # 3GB in KB
                return R()
        self.assertEqual(self.zt._rss_now_mb(run=FakeRun()), 3072)

    async def test_failure_returns_minus_one(self):
        class FakeRun:
            def __call__(self, *a, **kw):
                raise OSError("sandbox")
        self.assertEqual(self.zt._rss_now_mb(run=FakeRun()), -1)

    async def test_real_measurement_of_the_own_process(self):
        # smoke test: where ps is available (the poller never runs
        # sandboxed) the RSS is positive and healthy. Under sandbox it
        # returns -1 → skip.
        mb = self.zt._rss_now_mb()
        if mb == -1:
            self.skipTest("ps no disponible en este entorno (sandbox)")
        self.assertGreaterEqual(mb, 5)
        self.assertLess(mb, 4096)


if __name__ == "__main__":
    unittest.main()
