"""F46 — «CoT R.C» labels and ONE button box per razonada (Damian,
20/10: "better if the cots are numbered starting from 1 and their
successors" → R.C chosen; "the first reasoning gets its boxes, then it
moves on with code diffs commands, then reasons again and a second
batch of boxes appears there").

R = razonada (a thinking block), C = a 3000-char box inside it: never
two «CoT 1», and the grouping shows in the label. Each razonada is born
with its first box sealed at ITS own chronological position in the chat
(not everything piled up at the top) and closes with its own final
state; the final message is the INDEX with all the buttons. The tap
edits the message of ITS razonada (per-box phs in the cache).
"""
import json
import datetime
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
        # razonada 1 → 1.1/1.2 · razonada 2 → 2.1/2.2/2.3: unique and grouped
        self.assertEqual(rotulos, ["CoT 1.1", "CoT 1.2", "CoT 2.1",
                                   "CoT 2.2", "CoT 2.3"])

    def test_raz_de_y_texto_razonada(self):
        self.assertEqual(rz._raz_de("CoT 12.3"), 12)
        self.assertEqual(rz._raz_de("CoT 1.1"), 1)
        self.assertEqual(rz._raz_de("CoT 7"), 0)      # old-style label
        self.assertEqual(rz._raz_de("chau"), 0)
        t = rz.texto_razonada("x<y", 2, 3, 9500)
        self.assertIn("pass 2", t)
        self.assertIn("3 boxes", t)
        self.assertIn("9500 chars", t)
        self.assertIn("x&lt;y", t)                    # escaped

    def test_botones_indices(self):
        # F49: buttons for arbitrary global indices (the boxes that live
        # in ONE message — the razonada's own or the late ones at close)
        rot = ["CoT 1.1", "CoT 1.2", "CoT 2.1", "CoT 3.1"]
        kbs = rz.botones_indices(SID, T0, rot, [2, 4])
        plano = [b for fila in kbs for b in fila]
        self.assertEqual([b["text"] for b in plano], ["CoT 1.2", "CoT 3.1"])
        self.assertEqual([b["callback_data"] for b in plano],
                         ["ct:%s:%d:2" % (SID, T0), "ct:%s:%d:4" % (SID, T0)])
        self.assertEqual(rz.botones_indices(SID, T0, rot, []), [])
        t = rz.texto_rezagadas("x<y", 2, T0)
        self.assertIn("2 boxes as the turn closed", t)
        self.assertIn("x&lt;y", t)
        # F51a: 📥 + time anchor — the close must not read as another turn
        self.assertTrue(t.startswith("📥 🧠"))
        con_hora = datetime.datetime.fromtimestamp(float(T0)).strftime("%H:%M")
        self.assertIn(con_hora, t)

    def test_botones_slice_por_razonada(self):
        kbs = rz.botones_cajas(SID, T0, ["CoT 2.1", "CoT 2.2"], ini=3)
        plano = [b for fila in kbs for b in fila]
        # the LABEL is local to the razonada, the callback carries the global one
        self.assertEqual([b["text"] for b in plano], ["CoT 2.1", "CoT 2.2"])
        self.assertEqual([b["callback_data"] for b in plano],
                         ["ct:%s:%d:3" % (SID, T0), "ct:%s:%d:4" % (SID, T0)])
        for b in plano:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)


class TestTapEditaElMensajeDeSuRazonada(unittest.TestCase):
    """The cache stores phs (one message_id per box): the tap on box n
    edits the message of the razonada that owns n — not the last one
    (the single-ph fallback and the edit itself live in test_f40)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_phs_por_caja(self):
        phs = rz._phs_de(["CoT 1.1", "CoT 1.2", "CoT 2.1"], {1: 111, 2: 222})
        self.assertEqual(phs, [111, 111, 222])
        # razonada without its own message (born in the closing fetch): None
        self.assertEqual(rz._phs_de(["CoT 1.1", "CoT 3.1"], {1: 111}),
                         [111, None])


if __name__ == "__main__":
    unittest.main()
