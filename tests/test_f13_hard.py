"""F13 — hardening del ProtoClient (repasada senior 2026-10-13).

1. LEAK: request() que timeoutea dejaba la respuesta tardía retenida en
   self.responses PARA SIEMPRE — cada árbol de un session/events lento suma
   decenas de MB y los polls cada 2s apilaban GBs (pico real 3561MB).
   Ahora: rid abandonado → _dispatch DESCARTA la respuesta tardía.
2. Cap defensivo: nunca más de 8 responses vivas.
3. CPU: el reader re-intentaba raw_decode por CADA chunk de 64KB (O(n²),
   ~1.8GB de escaneo por frame de 15MB). Ahora solo con señal de fin
   probable ('\\n' crudo — json los escapa — o '.') o silencio de 250ms.
4. Frames sin \\n final (fallback de silencio) deben parsear igual.
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
            # la respuesta tardía llega al reader (Condition → despertar
            # reactivo): esperar su LIBERACIÓN por condición, no por tiempo
            await asynkit.wait_until(
                lambda: not c.abandoned, timeout=10,
                msg="la respuesta tardía debió liberar el rid abandonado")
            self.assertEqual(c.responses, {})          # descartada, no retenida
            self.assertEqual(len(c.abandoned), 0)      # y liberada del set
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
    """5. GUARD ANTI-FLOATS (auditoría profunda 20/10): el regex viejo
    miraba el json.dumps serializado — «: 1.5» dentro de un STRING
    matcheaba, el AssertionError se tragaba en silencio y el send del
    turno se perdía hasta el timeout. Un prompt «medición: 1.5 kg»
    mataba el turno sin rastro. Ahora el guard recorre el ÁRBOL: solo
    un float NUMÉRICO real no viaja (JSON-numérico no acepta \\u002e)."""

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
            self.assertEqual(r["error"]["code"], -2)  # no viaja, ni corrompe
        finally:
            c.close()


class TestReaderSenalDeFin(unittest.IsolatedAsyncioTestCase):
    async def test_frame_15mb_parsea_rapido(self):
        """CPU-guard: antes del fix eran ~1.8GB de re-scanneos por frame."""
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
        """Respuesta de 1.2MB SIN \\n final: el parseo debe llegar por el
        fallback de silencio (250ms de pipe mudo) — regresión del reader
        nuevo. El fallback de VOLUMEN se probó y se sacó: reintenta joins a
        mitad de frame → O(n²) (pico medido 681MB por frame de 12MB)."""
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
        """Caso borde: frame chico sin \\n ni '.' — al cerrar el pipe, el
        flush final del reader lo despacha (path 'resto')."""
        c = _client()
        try:
            r = c.request("chico/mudo", {"no_newline": 1}, timeout=15)
            # puede llegar por fallback o por el flush de EOF del server
            self.assertFalse(r.get("error"),
                             "el frame mudo se perdió: %r" % r.get("error"))
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
