"""F46 — rótulos «CoT R.C» y UNA caja de botones por razonada (Damian,
20/10: "mejor que los cot se enumeren a partir de 1 y sus sucesores" →
elegido R.C; "que primer razonamiento tenga sus cajas, luego avanza con
código diffs comandos, luego razona de nuevo y ahí aparece una segunda
tanda de cajas").

R = razonada (bloque de pensado), C = caja de 3000 chars dentro de ella:
nunca dos «CoT 1», y el agrupamiento se ve en el rótulo. Cada razonada
nace con su primer caja sellada en SU posición cronológica del chat (no
todo acumulado arriba) y cierra con estado final propio; el mensaje final
es el ÍNDICE con todos los botones. El tap edita el mensaje de SU
razonada (phs por caja en el cache).
"""
import json
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402

CAP = rz.COT_CAJA_CAP
T0 = 1760000000
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"


class TestRotulosRC(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_rotulos_unicos_con_agrupamiento(self):
        def _m(rol, *parts):
            return {"info": {"role": rol}, "parts": list(parts)}
        msgs = [
            _m("user", {"type": "text", "text": "p"}),
            _m("assistant", {"type": "reasoning", "text": "a" * (CAP + 5)},
               {"type": "tool", "tool": "Bash"}),
            _m("assistant", {"type": "reasoning",
                             "text": "b" * (2 * CAP + 10)},
               {"type": "text", "text": "fin"}),
        ]
        cajas, rotulos = rz.cajas_del_turno(msgs)
        self.assertEqual([len(c) for c in cajas], [CAP, 5, CAP, CAP, 10])
        # razonada 1 → 1.1/1.2 · razonada 2 → 2.1/2.2/2.3: únicos y con grupo
        self.assertEqual(rotulos, ["CoT 1.1", "CoT 1.2", "CoT 2.1",
                                   "CoT 2.2", "CoT 2.3"])

    def test_raz_de_y_texto_razonada(self):
        self.assertEqual(rz._raz_de("CoT 12.3"), 12)
        self.assertEqual(rz._raz_de("CoT 1.1"), 1)
        self.assertEqual(rz._raz_de("CoT 7"), 0)      # rótulo viejo
        self.assertEqual(rz._raz_de("chau"), 0)
        t = rz.texto_razonada("x<y", 2, 3, 9500)
        self.assertIn("razonada 2", t)
        self.assertIn("3 cajas", t)
        self.assertIn("9500 chars", t)
        self.assertIn("x&lt;y", t)                    # escapado

    def test_botones_indices(self):
        # F49: botones para índices globales arbitrarios (las cajas que
        # viven en UN mensaje — la razonada propia o las tardías del cierre)
        rot = ["CoT 1.1", "CoT 1.2", "CoT 2.1", "CoT 3.1"]
        kbs = rz.botones_indices(SID, T0, rot, [2, 4])
        plano = [b for fila in kbs for b in fila]
        self.assertEqual([b["text"] for b in plano], ["CoT 1.2", "CoT 3.1"])
        self.assertEqual([b["callback_data"] for b in plano],
                         ["ct:%s:%d:2" % (SID, T0), "ct:%s:%d:4" % (SID, T0)])
        self.assertEqual(rz.botones_indices(SID, T0, rot, []), [])
        t = rz.texto_rezagadas("x<y", 2)
        self.assertIn("2 cajas al cierre", t)
        self.assertIn("x&lt;y", t)

    def test_botones_slice_por_razonada(self):
        kbs = rz.botones_cajas(SID, T0, ["CoT 2.1", "CoT 2.2"], ini=3)
        plano = [b for fila in kbs for b in fila]
        # el RÓTULO es local a la razonada, el callback lleva el global
        self.assertEqual([b["text"] for b in plano], ["CoT 2.1", "CoT 2.2"])
        self.assertEqual([b["callback_data"] for b in plano],
                         ["ct:%s:%d:3" % (SID, T0), "ct:%s:%d:4" % (SID, T0)])
        for b in plano:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)


class TestTapEditaElMensajeDeSuRazonada(unittest.TestCase):
    """El cache guarda phs (un message_id por caja): el tap de la caja n
    edita el mensaje de la razonada dueña de n — no el último (el fallback
    al ph único y el edit en sí viven en test_f40)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_phs_por_caja(self):
        phs = rz._phs_de(["CoT 1.1", "CoT 1.2", "CoT 2.1"], {1: 111, 2: 222})
        self.assertEqual(phs, [111, 111, 222])
        # razonada sin mensaje propio (nacida en el fetch de cierre): None
        self.assertEqual(rz._phs_de(["CoT 1.1", "CoT 3.1"], {1: 111}),
                         [111, None])


if __name__ == "__main__":
    unittest.main()
