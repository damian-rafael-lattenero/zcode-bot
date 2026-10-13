"""F11 — medir y amortiguar el pico de RSS del poller durante el live.

Hallazgo del turno "Testing 123" (2026-10-13 15:44): poller recién nacido
(rss=20MB) pickeó a 3561MB en 34 s con un frame de session/events de SOLO
15MB. ru_maxrss (pico histórico) no distingue transitorio de retenido:
1. _rss_now_mb(): RSS ACTUAL via ps (el poller NO corre bajo sandbox —
   solo sus runtimes — así que ps le funciona).
2. _poll_cadence(): el protocolo RECHAZA afterEventId (verificado en vivo:
   ZodError unrecognized_keys) → sin incremental, la palanca es la frec:
   frame >2MB → poll cada 5 s en vez de 2 s.
"""
import os
import unittest

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


class TestPollCadence(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_frame_chico_cadencia_normal(self):
        self.assertEqual(self.zt._poll_cadence(64 * 1024), 2.0)
        self.assertEqual(self.zt._poll_cadence(0), 2.0)

    def test_frame_grande_cadencia_lenta(self):
        self.assertEqual(self.zt._poll_cadence(15 * 1024 * 1024), 5.0)
        justo = self.zt.LIVE_BIG_FRAME
        self.assertEqual(self.zt._poll_cadence(justo), 2.0)   # umbral excluyente
        self.assertEqual(self.zt._poll_cadence(justo + 1), 5.0)


class TestRssNow(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_parsea_la_salida_de_ps(self):
        class FakeRun:
            def __call__(self, *a, **kw):
                class R:
                    stdout = "  3145728 \n"  # 3GB en KB
                return R()
        self.assertEqual(self.zt._rss_now_mb(run=FakeRun()), 3072)

    def test_fallo_devuelve_menos_uno(self):
        class FakeRun:
            def __call__(self, *a, **kw):
                raise OSError("sandbox")
        self.assertEqual(self.zt._rss_now_mb(run=FakeRun()), -1)

    def test_medicion_real_del_propio_proceso(self):
        # humo: donde ps está disponible (el poller nunca corre sandboxeado)
        # el RSS es positivo y sanito. Bajo sandbox devuelve -1 → skip.
        mb = self.zt._rss_now_mb()
        if mb == -1:
            self.skipTest("ps no disponible en este entorno (sandbox)")
        self.assertGreaterEqual(mb, 5)
        self.assertLess(mb, 4096)


if __name__ == "__main__":
    unittest.main()
