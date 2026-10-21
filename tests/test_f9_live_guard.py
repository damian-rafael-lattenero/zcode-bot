"""F9 — two incidents from 2026-10-13, each with its regression test:

1. Poller OOM: a 3.2GB RSS peak in a turn with barely 329 events
   (session/events replay with images). The GIANT frame got re-parsed
   every 2 s → LIVE_FRAME_TRIP turns live off after ONE read and
   _live_skip_mark leaves a per-session flag (compact clears it).
2. Runtime suicide: the headless turn ran `kill -TERM <pid>` on its own
   host ("worker duplicado") and died with «Turn was cancelled»
   → _headless_text appends the guardrail to every headless prompt.
"""
import os
import sys
import time
import unittest

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import zcode_proto as zp  # noqa: E402


class TestLiveSkipFlag(unittest.IsolatedAsyncioTestCase):
    """Per-session 'giant replay' flag: mark → check → expire/clear."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_mark_y_check_fresco(self):
        self.zt._live_skip_mark("sess_X")
        self.assertTrue(self.zt._live_skip_check("sess_X"))
        self.assertFalse(self.zt._live_skip_check("sess_OTRA"))

    async def test_flag_expira_tras_ttl(self):
        ahora = time.time()
        self.zt._live_skip_mark("sess_X", now=ahora)
        # just marked: valid; at TTL+1s: expired
        self.assertTrue(self.zt._live_skip_check("sess_X", now=ahora + 1))
        self.assertFalse(self.zt._live_skip_check(
            "sess_X", now=ahora + self.zt.LIVE_SKIP_TTL + 1))

    async def test_clear_lo_saca_y_escribe_archivo(self):
        self.zt._live_skip_mark("sess_X")
        self.zt._live_skip_mark("sess_Y")
        self.zt._live_skip_clear("sess_X")
        self.assertFalse(self.zt._live_skip_check("sess_X"))
        self.assertTrue(self.zt._live_skip_check("sess_Y"))  # the others remain
        # the file persists only the survivor
        import json
        with open(self.zt._live_skip_path()) as f:
            self.assertEqual(set(json.load(f)), {"sess_Y"})

    async def test_estado_corrupto_no_explota(self):
        with open(self.zt._live_skip_path(), "w") as f:
            f.write("no-es-json{")
        self.assertFalse(self.zt._live_skip_check("sess_X"))


class TestHeadlessGuard(unittest.IsolatedAsyncioTestCase):
    """The anti-suicide guardrail goes in ALL headless sends."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_se_apendea_al_prompt(self):
        out = self.zt._headless_text(
            "arreglá el bug", {"headless_guard": True})  # F34c: default in DEFAULTS
        self.assertTrue(out.startswith("arreglá el bug"))
        self.assertIn("HEADLESS", out)
        self.assertIn("Turn was cancelled", out)
        self.assertIn("/nuclear", out)

    async def test_es_idempotente(self):
        una = self.zt._headless_text("hola", {})
        dos = self.zt._headless_text(una, {})
        self.assertEqual(una, dos)  # queue replay: not duplicated

    async def test_se_apaga_con_cfg(self):
        out = self.zt._headless_text("hola", {"headless_guard": False})
        self.assertEqual(out, "hola")

    async def test_prompt_vacio_sigue_valido(self):
        out = self.zt._headless_text("", {"headless_guard": True})
        self.assertTrue(out.strip())


class TestMaxFrameProtoClient(unittest.IsolatedAsyncioTestCase):
    """The ProtoClient's max_frame measures the pending buffer: a
    response with a 30MB pad must leave it above LIVE_FRAME_TRIP (25MB).
    That way the gateway can trip the live switch without repeating the
    giant parse."""

    async def test_frame_gigante_se_mide(self):
        c = zp.ProtoClient(
            argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])
        try:
            r = c.request("session/events",
                          {"pad_bytes": 30 * 1024 * 1024}, timeout=60)
            self.assertFalse(r.get("error"))
            self.assertGreater(c.max_frame, 25 * 1024 * 1024)
        finally:
            c.close()

    async def test_frames_chicos_no_miden_gigante(self):
        c = zp.ProtoClient(
            argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])
        try:
            for i in range(5):
                r = c.request("probe/chico%d" % i, {}, timeout=15)
                self.assertFalse(r.get("error"))
            self.assertLess(c.max_frame, 1024 * 1024)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
