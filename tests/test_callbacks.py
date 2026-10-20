"""R5 — la tabla de callbacks: UNA traducción, cubierto el if-chain
histórico completo (cada rama de updates._cb_text viejo, textual) para
que la mudanza a commands/callbacks.py sea una equivalencia PROBADA, no
supuesta. Los formatos viejos (t:ses_, task:N) siguen vivos en los chats
del dueño — acá quedan clavados.
"""
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

from zcode_bot.commands import callbacks as cb  # noqa: E402
from zcode_bot import updates  # noqa: E402

SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"

# (callback_data, texto esperado) — una fila por rama del if-chain viejo
CASOS = [
    ("t:%s" % SID, "/task @%s" % SID),
    ("t:ses_viejo", "/task @ses_viejo"),          # formato corto viejo
    ("s:%s" % SID, "/stop @%s" % SID),
    ("c:%s" % SID, "/compact @%s" % SID),
    ("mo:plan", "/mode plan"),
    ("qgo:%s:2" % SID, "/qgo %s:2" % SID),
    ("qdel:%s:0" % SID, "/qdel %s:0" % SID),
    ("retry", "/retry"),
    ("m:glm-5.3", "/model glm-5.3"),
    ("th:max", "/thinking max"),
    ("task:3", "/all_tasks 3"),                   # vocabulario viejo
    ("ans:mirá este plan", "mirá este plan"),     # respuesta textual
    ("yes", "si"),
    ("no", "no"),
    ("nuc:go", "/nuclear go"),
    ("nuc:no", "/nuclear no"),
    ("dep:go", "/deploy go"),
    ("dep:no", "/deploy no"),
    ("df:2:abc1234", "/diff 2 abc1234"),
    ("dx:3:1", "/diff dirty 3 1"),
    ("dc:abc1234", "/diff commit abc1234"),
    ("dl", "/diff log"),
    ("dw", "/diff"),
    ("ct:%s:176:3:2" % SID, "/cotb %s 176 3 2" % SID),
]


class TestTablaCallbacks(unittest.TestCase):
    def test_cada_rama_historica_traduce_igual(self):
        for data, esperado in CASOS:
            self.assertEqual(cb.texto_de_callback(data), esperado, data)
            # el alias histórico de updates sigue siendo LA misma función
            self.assertEqual(updates._cb_text(data), esperado, data)

    def test_desconocido_y_planfix_caen_a_texto_libre(self):
        self.assertEqual(cb.texto_de_callback("hola mundo" * 20),
                         "hola mundo" * 20[:64] if False else
                         ("hola mundo" * 20)[:64])
        # planfix (botón «✏️ Agregar algo al plan»): passthrough explícito
        self.assertEqual(cb.texto_de_callback("planfix"), "planfix")

    def test_es_respuesta_de_pregunta(self):
        self.assertTrue(cb.es_respuesta_de_pregunta("ans:x"))
        self.assertTrue(cb.es_respuesta_de_pregunta("yes"))
        self.assertTrue(cb.es_respuesta_de_pregunta("no"))
        self.assertFalse(cb.es_respuesta_de_pregunta("si"))  # tipeado, no botón
        self.assertFalse(cb.es_respuesta_de_pregunta("retry"))
        self.assertFalse(cb.es_respuesta_de_pregunta(""))
        self.assertFalse(cb.es_respuesta_de_pregunta("no:false"))


if __name__ == "__main__":
    unittest.main()
