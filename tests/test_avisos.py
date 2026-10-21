"""F36 — avisos: the «choice received» ack component.

A button's cycle has two feedbacks: the native 👀 (Telegram took it)
and this component («the app ACCEPTED the request»). One single format
for compact (F35), stop and reply-to-agent — the compact tests live in
test_timeouts_sesion (fully faked flow); this file holds the pure
component's contract.
"""
import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

from zcode_bot import avisos  # noqa: E402

CFG = {"token": "T", "chat_id": 1}


class TestEleccionRecibida(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.p = mock.patch.object(avisos.uhttp, "send",
                                   lambda cfg, text, loud=None, markup=None:
                                   (self.sends.append(text), True)[1])
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_formato_con_detalle(self):
        avisos.eleccion_recibida(CFG, "compactando «CV»",
                                 "te aviso al terminar")
        self.assertEqual(self.sends,
                         ["📬 Your choice arrived: compactando «CV»"
                          " — te aviso al terminar"])

    def test_formato_sin_detalle(self):
        avisos.eleccion_recibida(CFG, "frenado «testing»")
        self.assertEqual(self.sends, ["📬 Your choice arrived: frenado «testing»"])

    def test_escapa_html_del_que(self):
        avisos.eleccion_recibida(CFG, "frenado «a<b»")
        self.assertIn("a&lt;b", self.sends[0])
        self.assertNotIn("a<b", self.sends[0])

    def test_siempre_mudo(self):
        """The buzz belongs to the RESULT: the ack travels with loud=False."""
        captura = {}
        with mock.patch.object(avisos.uhttp, "send",
                               lambda cfg, text, loud=None, markup=None:
                                   captura.update(loud=loud)):
            avisos.eleccion_recibida(CFG, "x")
        self.assertFalse(captura["loud"])


if __name__ == "__main__":
    unittest.main()
