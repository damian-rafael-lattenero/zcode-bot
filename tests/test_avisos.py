"""F36 — avisos: el componente del ack «llegó tu elección».

El ciclo de un botón tiene dos feedbacks: el 👀 nativo (Telegram lo
tomó) y este componente («la app ACEPTÓ el pedido»). Un solo formato
para compact (F35), stop y respuesta-al-agente — los tests de compact
viven en test_timeouts_sesion (flujo completo fakeado); acá va el
contrato del componente puro.
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
                         ["📬 Llegó tu elección: compactando «CV»"
                          " — te aviso al terminar"])

    def test_formato_sin_detalle(self):
        avisos.eleccion_recibida(CFG, "frenado «testing»")
        self.assertEqual(self.sends, ["📬 Llegó tu elección: frenado «testing»"])

    def test_escapa_html_del_que(self):
        avisos.eleccion_recibida(CFG, "frenado «a<b»")
        self.assertIn("a&lt;b", self.sends[0])
        self.assertNotIn("a<b", self.sends[0])

    def test_siempre_mudo(self):
        """El buzz es del RESULTADO: el acuse viaja con loud=False."""
        captura = {}
        with mock.patch.object(avisos.uhttp, "send",
                               lambda cfg, text, loud=None, markup=None:
                                   captura.update(loud=loud)):
            avisos.eleccion_recibida(CFG, "x")
        self.assertFalse(captura["loud"])


if __name__ == "__main__":
    unittest.main()
