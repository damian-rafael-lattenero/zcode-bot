"""F42 — el CoT de los turnos del CELU en botones (como la Mac, F41).

Damian: "yo hablaba de conversación del teléfono, no de la Mac — igual
quiero las dos". Los turnos del celu ya streameaban el CoT como TEXTO
(cajas «CoT N» de 3000 chars); ahora cada fase de pensado que cierra
(pasa a escribir o a llamar una tool) agrega su BOTÓN a una caja que
crece, con el mismo cache/ct:/cotb de la Mac.
"""
import json
import os
import sys
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402
from zcode_bot.live_view import LiveTurnView  # noqa: E402


class _FakeC:
    def __init__(self, evs=None):
        self.evs = evs or []
        self.pedidos = []

    def request(self, method, params=None, timeout=None):
        self.pedidos.append((method, dict(params or {})))
        return {"result": {"events": [dict(e) for e in self.evs]}}


class _Th:
    def __init__(self, n=2):
        self.n = n

    def is_alive(self):
        self.n -= 1
        return self.n > 0


def _ev(eid, kind, delta=""):
    p = {"kind": kind}
    if delta:
        p["delta"] = delta
    return {"eventId": eid, "payload": p}


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1)
    cfg.update(extra)
    return cfg


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def _view(self, c, cfg=None, **dep_extra):
        self.sends = []       # (texto, markup)
        self.live_sends = []
        self.edits = []       # (mid, texto, markup)
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append((text, markup)), 601)[1],
            "live_send": lambda cfg, text:
                (self.live_sends.append(text), 620)[1],
            "live_edit": lambda cfg, mid, text, markup=None:
                (self.edits.append((mid, text, markup)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: None,
        }
        deps.update(dep_extra)
        return LiveTurnView(c, cfg or _cfg(self.zt, cot_botones=True),
                            self.T1, 1760000000.0, "testing", 777, _Th(2),
                            last_id=None, deps=deps)

    def _botones(self, kb):
        return [b["text"] for fila in (kb or {}).get("inline_keyboard", [])
                for b in fila]


# dos fases de pensado: una cierra al llamar la tool, otra al escribir
_FASES = [
    _ev(1, "reasoning_delta", "pienso antes de la tool "),
    _ev(2, "tool_input_start"), _ev(3, "tool_call"),
    _ev(4, "reasoning_delta", "pienso tras el resultado "),
    _ev(5, "text_delta", "la respuesta"),
]


class TestCotBotonesCelu(_Base):
    async def test_bloques_como_botones_que_crecen(self):
        v = self._view(_FakeC(list(_FASES)))
        cot, texto, pasos, live_id = v.run()
        # la caja de botones NACE con el primer bloque (send con teclado)
        primera = [s for s in self.sends if s[1]][0]
        self.assertIn("1 bloque listo", primera[0])
        self.assertEqual(self._botones(primera[1]), ["CoT 1"])
        # el segundo bloque EDITA el MISMO mensaje: teclado crecido
        edits_bloques = [e for e in self.edits
                         if e[2] and "bloque" in e[1]]
        self.assertEqual(self._botones(edits_bloques[-1][2]),
                         ["CoT 1", "CoT 2"])
        # final: texto Razonamiento con TODOS los botones y cache finalizado
        final = [e for e in self.edits
                 if e[2] and "Razonamiento" in e[1]][-1]
        self.assertIn("2 bloques", final[1])
        self.assertEqual(self._botones(final[2]), ["CoT 1", "CoT 2"])
        with open(rz.cache_path(self.T1, int(v.t0))) as f:
            d = json.load(f)
        self.assertEqual(d["bloques"], ["pienso antes de la tool ",
                                        "pienso tras el resultado "])
        self.assertTrue(d["finalizado"])
        # el tap funciona contra el cache del celu (mismo camino que la Mac)
        out = rz.mostrar(self.T1, int(v.t0), 1)
        self.assertIn("<pre>pienso antes de la tool </pre>", out)

    async def test_el_texto_de_cot_stream_no_se_manda(self):
        v = self._view(_FakeC(list(_FASES)),
                       cfg=_cfg(self.zt, cot_botones=True, cot_stream=True))
        v.run()
        self.assertFalse(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any("análisis terminado" in s[0] for s in self.sends))
        # cot_full queda ENTERO en el cache (no se libera por el flush)
        self.assertEqual(v.cot_bloques[0] + v.cot_bloques[1],
                         "pienso antes de la tool pienso tras el resultado ")

    async def test_knob_apagado_comporta_como_antes(self):
        v = self._view(_FakeC(list(_FASES)),
                       cfg=_cfg(self.zt, cot_botones=False, cot_stream=True))
        v.run()
        # el CoT viaja como texto (cajas permanentes) y no hay botones
        self.assertTrue(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any(s[1] for s in self.sends))
        self.assertFalse(any(e[2] and "CoT" in str(e[2])
                             for e in self.edits))

    async def test_turno_sin_razonamiento_no_crea_caja(self):
        v = self._view(_FakeC([_ev(1, "text_delta", "directo")]))
        v.run()
        self.assertFalse(any(s[1] for s in self.sends))
        self.assertFalse(os.path.exists(rz.cache_path(self.T1, int(v.t0))))


if __name__ == "__main__":
    unittest.main()
