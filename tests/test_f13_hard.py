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
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import zcode_proto as zp  # noqa: E402


def _client():
    return zp.ProtoClient(
        argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])


class TestRespuestasAbandonadas(unittest.IsolatedAsyncioTestCase):
    async def test_respuesta_tardia_tras_timeout_se_descarta(self):
        c = _client()
        try:
            r = c.request("lento/uno", {"slow_ms": 900}, timeout=0.3)
            self.assertEqual(r["error"]["code"], -2)  # timeout
            self.assertEqual(len(c.abandoned), 1)
            # the late response reaches the reader (Condition → reactive
            # wakeup): wait for its RELEASE by condition, not by time
            await asynkit.wait_until(
                lambda: not c.abandoned, timeout=10,
                msg="la respuesta tardía debió liberar el rid abandonado")
            self.assertEqual(c.responses, {})          # discarded, not retained
            self.assertEqual(len(c.abandoned), 0)      # and released from the set
        finally:
            c.close()

    async def test_cap_defensivo_de_responses(self):
        c = _client()
        try:
            for i in range(12):
                c._dispatch({"id": "huerfano%d" % i, "result": {"n": i}})
            self.assertLessEqual(len(c.responses), 8)
        finally:
            c.close()

    async def test_request_normal_siguiendo_pop(self):
        c = _client()
        try:
            r = c.request("normal/uno", {}, timeout=15)
            self.assertEqual((r.get("result") or {}).get("echo"), "normal/uno")
            self.assertEqual(c.responses, {})
        finally:
            c.close()


class TestGuardDeFloats(unittest.IsolatedAsyncioTestCase):
    """5. ANTI-FLOATS GUARD (deep audit 20/10): the old regex looked at
    the serialized json.dumps — «: 1.5» inside a STRING matched, the
    AssertionError was silently swallowed and the turn's send was lost
    until the timeout. A prompt like «medición: 1.5 kg» killed the turn
    without a trace. Now the guard walks the TREE: only a real NUMERIC
    float does not travel (JSON-numeric does not accept \\u002e)."""

    async def test_punto_flotante_en_un_string_viaja(self):
        c = _client()
        try:
            r = c.request("eco/uno", {"content": "medición: 1.5 kg"},
                          timeout=10)
            self.assertNotIn("error", r)
            self.assertEqual((r.get("result") or {}).get("echo"), "eco/uno")
        finally:
            c.close()

    async def test_float_numerico_real_falla_limpio(self):
        c = _client()
        try:
            r = c.request("eco/dos", {"peso": 1.5}, timeout=3)
            self.assertEqual(r["error"]["code"], -2)  # does not travel, nor corrupt
        finally:
            c.close()


class TestReaderSenalDeFin(unittest.IsolatedAsyncioTestCase):
    async def test_frame_15mb_parsea_rapido(self):
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
            self.assertLess(dt, 20, "frame de 15MB tardó %.1fs" % dt)
        finally:
            c.close()

    async def test_frame_sin_newline_via_fallback_silencio(self):
        """A 1.2MB response WITHOUT a trailing \\n: parsing must arrive
        via the silence fallback (250ms of mute pipe) — regression test
        for the new reader. The VOLUME fallback was tried and removed:
        it retries joins mid-frame → O(n²) (measured peak 681MB per 12MB
        frame)."""
        c = _client()
        try:
            r = c.request("sin/salto", {"pad_bytes": 1200 * 1024,
                                        "no_newline": 1}, timeout=30)
            self.assertFalse(r.get("error"))
            self.assertGreater(len((r.get("result") or {}).get("pad", "")),
                               1000 * 1024)
        finally:
            c.close()

    async def test_mini_frames_sin_senal_no_se_pierden(self):
        """Edge case: small frame with no \\n and no '.' — when the pipe
        closes, the reader's final flush dispatches it (the 'resto'
        path)."""
        c = _client()
        try:
            r = c.request("chico/mudo", {"no_newline": 1}, timeout=15)
            # it may arrive via the fallback or via the server's EOF flush
            self.assertFalse(r.get("error"),
                             "el frame mudo se perdió: %r" % r.get("error"))
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
