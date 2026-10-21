"""F13 — ProtoClient hardening (senior review 2026-10-13).

1. LEAK: a request() that timed out left the late response retained in
   self.responses FOREVER — each tree of a slow session/events adds tens
   of MB and the polls every 2s piled up GBs (real peak 3561MB).
   Now: abandoned rid → _dispatch DISCARDS the late response.
2. Defensive cap: never more than 8 live responses.
3. CPU: the reader retried raw_decode for EVERY 64KB chunk (O(n²),
   ~1.8GB of scanning per 15MB frame). Now only on a probable end
   signal (raw '\\n' — json escapes those — or '.') or 250ms of silence.
4. Frames without a trailing \\n (silence fallback) must parse the same.
"""
import os
import sys
import time
import unittest

import asynkit

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import zcode_proto as zp  # noqa: E402


def _client():
    return zp.ProtoClient(
        argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])


class TestAbandonedReplies(unittest.IsolatedAsyncioTestCase):
    async def test_late_reply_after_timeout_is_discarded(self):
        c = _client()
        try:
            r = c.request("slow/one", {"slow_ms": 900}, timeout=0.3)
            self.assertEqual(r["error"]["code"], -2)  # timeout
            self.assertEqual(len(c.abandoned), 1)
            # the late response reaches the reader (Condition → reactive
            # wakeup): wait for its RELEASE by condition, not by time
            await asynkit.wait_until(
                lambda: not c.abandoned, timeout=10,
                msg="the late reply should have released the abandoned rid")
            self.assertEqual(c.responses, {})          # discarded, not retained
            self.assertEqual(len(c.abandoned), 0)      # and released from the set
        finally:
            c.close()

    async def test_defensive_cap_of_responses(self):
        c = _client()
        try:
            for i in range(12):
                c._dispatch({"id": "orphan%d" % i, "result": {"n": i}})
            self.assertLessEqual(len(c.responses), 8)
        finally:
            c.close()

    async def test_request_normal_following_pop(self):
        c = _client()
        try:
            r = c.request("normal/one", {}, timeout=15)
            self.assertEqual((r.get("result") or {}).get("echo"), "normal/one")
            self.assertEqual(c.responses, {})
        finally:
            c.close()


class TestGuardDeFloats(unittest.IsolatedAsyncioTestCase):
    """5. ANTI-FLOATS GUARD (deep audit 20/10): the old regex looked at
    the serialized json.dumps — «: 1.5» inside a STRING matched, the
    AssertionError was silently swallowed and the turn's send was lost
    until the timeout. A prompt like «measurement: 1.5 kg» killed the turn
    without a trace. Now the guard walks the TREE: only a real NUMERIC
    float does not travel (JSON-numeric does not accept \\u002e)."""

    async def test_float_in_a_string_travels(self):
        c = _client()
        try:
            r = c.request("echo/one", {"content": "measurement: 1.5 kg"},
                          timeout=10)
            self.assertNotIn("error", r)
            self.assertEqual((r.get("result") or {}).get("echo"), "echo/one")
        finally:
            c.close()

    async def test_real_numeric_float_fails_clean(self):
        c = _client()
        try:
            r = c.request("echo/two", {"weight": 1.5}, timeout=3)
            self.assertEqual(r["error"]["code"], -2)  # does not travel, nor corrupt
        finally:
            c.close()


class TestReaderEndSignal(unittest.IsolatedAsyncioTestCase):
    async def test_15mb_frame_parses_fast(self):
        """CPU-guard: before the fix it was ~1.8GB of re-scans per frame."""
        c = _client()
        try:
            t0 = time.time()
            r = c.request("session/events",
                          {"pad_bytes": 15 * 1024 * 1024}, timeout=60)
            dt = time.time() - t0
            self.assertFalse(r.get("error"))
            self.assertEqual((r["result"]["echo"]), "session/events")
            self.assertGreater(c.max_frame, 15 * 1024 * 1024)
            self.assertLess(dt, 20, "the 15MB frame took %.1fs" % dt)
        finally:
            c.close()

    async def test_frame_without_newline_via_fallback_silence(self):
        """A 1.2MB response WITHOUT a trailing \\n: parsing must arrive
        via the silence fallback (250ms of mute pipe) — regression test
        for the new reader. The VOLUME fallback was tried and removed:
        it retries joins mid-frame → O(n²) (measured peak 681MB per 12MB
        frame)."""
        c = _client()
        try:
            r = c.request("no/newline", {"pad_bytes": 1200 * 1024,
                                        "no_newline": 1}, timeout=30)
            self.assertFalse(r.get("error"))
            self.assertGreater(len((r.get("result") or {}).get("pad", "")),
                               1000 * 1024)
        finally:
            c.close()

    async def test_mini_frames_without_signal_are_not_lost(self):
        """Edge case: small frame with no \\n and no '.' — when the pipe
        closes, the reader's final flush dispatches it (the 'rest'
        path)."""
        c = _client()
        try:
            r = c.request("tiny/silent", {"no_newline": 1}, timeout=15)
            # it may arrive via the fallback or via the server's EOF flush
            self.assertFalse(r.get("error"),
                             "the silent frame was lost: %r" % r.get("error"))
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
