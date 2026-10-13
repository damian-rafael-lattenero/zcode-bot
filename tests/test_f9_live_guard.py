"""F9 — dos incidentes del 2026-10-13, cada uno con su regresión:

1. OOM del poller: pico de 3.2GB de RSS en un turno con apenas 329 eventos
   (replay de session/events con imágenes). El frame GIGANTE se re-parsea
   cada 2 s → LIVE_FRAME_TRIP apaga el live tras UNA lectura y
   _live_skip_mark deja flag por sesión (el compact lo limpia).
2. Suicidio de runtime: el turno headless ejecutó `kill -TERM <pid>` sobre
   su propio huésped ("worker duplicado") y murió con «Turn was cancelled»
   → _headless_text apendea el guardrail a cada prompt headless.
"""
import os
import sys
import time
import unittest

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import zcode_proto as zp  # noqa: E402


class TestLiveSkipFlag(unittest.TestCase):
    """Flag por sesión 'replay gigante': marcar → consultar → expirar/limpiar."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_mark_y_check_fresco(self):
        self.zt._live_skip_mark("sess_X")
        self.assertTrue(self.zt._live_skip_check("sess_X"))
        self.assertFalse(self.zt._live_skip_check("sess_OTRA"))

    def test_flag_expira_tras_ttl(self):
        ahora = time.time()
        self.zt._live_skip_mark("sess_X", now=ahora)
        # recién marcado: vigente; a TTL+1s: vencido
        self.assertTrue(self.zt._live_skip_check("sess_X", now=ahora + 1))
        self.assertFalse(self.zt._live_skip_check(
            "sess_X", now=ahora + self.zt.LIVE_SKIP_TTL + 1))

    def test_clear_lo_saca_y_escribe_archivo(self):
        self.zt._live_skip_mark("sess_X")
        self.zt._live_skip_mark("sess_Y")
        self.zt._live_skip_clear("sess_X")
        self.assertFalse(self.zt._live_skip_check("sess_X"))
        self.assertTrue(self.zt._live_skip_check("sess_Y"))  # las demás quedan
        # el archivo persiste solo el sobreviviente
        import json
        with open(self.zt._live_skip_path()) as f:
            self.assertEqual(set(json.load(f)), {"sess_Y"})

    def test_estado_corrupto_no_explota(self):
        with open(self.zt._live_skip_path(), "w") as f:
            f.write("no-es-json{")
        self.assertFalse(self.zt._live_skip_check("sess_X"))


class TestHeadlessGuard(unittest.TestCase):
    """El guardrail anti-suicidio va en TODOS los sends headless."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_se_apendea_al_prompt(self):
        out = self.zt._headless_text("arreglá el bug", {})
        self.assertTrue(out.startswith("arreglá el bug"))
        self.assertIn("HEADLESS", out)
        self.assertIn("Turn was cancelled", out)
        self.assertIn("/nuclear", out)

    def test_es_idempotente(self):
        una = self.zt._headless_text("hola", {})
        dos = self.zt._headless_text(una, {})
        self.assertEqual(una, dos)  # reenvío de cola: no se duplica

    def test_se_apaga_con_cfg(self):
        out = self.zt._headless_text("hola", {"headless_guard": False})
        self.assertEqual(out, "hola")

    def test_prompt_vacio_sigue_valido(self):
        out = self.zt._headless_text("", {})
        self.assertTrue(out.strip())


class TestMaxFrameProtoClient(unittest.TestCase):
    """max_frame del ProtoClient mide el buffer pendiente: un response con
    pad de 30MB debe dejarlo por encima de LIVE_FRAME_TRIP (25MB). Así el
    gateway puede trippear el live sin repetir el parseo gigante."""

    def test_frame_gigante_se_mide(self):
        c = zp.ProtoClient(
            argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])
        try:
            r = c.request("session/events",
                          {"pad_bytes": 30 * 1024 * 1024}, timeout=60)
            self.assertFalse(r.get("error"))
            self.assertGreater(c.max_frame, 25 * 1024 * 1024)
        finally:
            c.close()

    def test_frames_chicos_no_miden_gigante(self):
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
