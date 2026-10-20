"""F42/F43 — el CoT de los turnos del CELU en cajas por tamaño.

Modelo de Damian: "una caja tiene un tamaño; cuando el CoT llega al tope
se llena la caja y se envía con su número; continúa y de nuevo frena en
el tope, y así van llegando botones hasta que el CoT termine; luego el
chat escribe o manda formulario; y cuando vuelva a pensar, manda CoT de
nuevo numerado desde 1". El worker alimenta un SelladorCot con los
reasoning_delta: la caja viaja como botón APENAS SE LLENA (en vivo, a
mitad de la razonada), no al cerrar la fase.
"""
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402
from zcode_bot.live_view import LiveTurnView  # noqa: E402

CAP = rz.COT_CAJA_CAP


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


# Una razonada que pasa el tope a mitad de delta (botón EN VIVO), cierra
# por tool (cola sellada), y una SEGUNDA razonada que reinicia en CoT 1.
_FASES = [
    _ev(1, "reasoning_delta", "x" * 2000),
    _ev(2, "reasoning_delta", "y" * 1500),   # total 3500 → CAJA 1 LLENA acá
    _ev(3, "tool_input_start"), _ev(4, "tool_call"),  # cierra: cola = caja 2
    _ev(5, "reasoning_delta", "z" * 300),    # 2ª razonada…
    _ev(6, "text_delta", "la respuesta"),    # …cierra al escribir: caja CoT 1
]


class TestCajasPorTamano(_Base):
    async def test_la_caja_viaja_apenas_se_llena_y_reinicia(self):
        v = self._view(_FakeC(list(_FASES)))
        cot, texto, pasos, live_id = v.run()
        # la caja de botones NACE con la primera LLENA (sin mensaje vacío)
        primera = [s for s in self.sends if s[1]][0]
        self.assertIn("1 caja", primera[0])
        self.assertIn("llenando la CoT 2", primera[0])
        self.assertEqual(self._botones(primera[1]), ["CoT 1"])
        # los textos sellados: caja 1 = primeros 3000 chars, caja 2 = cola 500
        self.assertEqual(v.cot_cajas[0], "x" * 2000 + "y" * 1000)
        self.assertEqual(v.cot_cajas[1], "y" * 500)
        # la 2ª razonada cierra al escribir → caja 3 con rótulo REINICIADO
        self.assertEqual(v.cot_rotulos, ["CoT 1", "CoT 2", "CoT 1"])
        final = [e for e in self.edits if e[2] and "Razonamiento" in e[1]][-1]
        self.assertIn("3 cajas", final[1])
        self.assertEqual(self._botones(final[2]),
                         ["CoT 1", "CoT 2", "CoT 1"])
        # el tap abre la caja 3 (la 2ª razonada) por el MISMO camino que la Mac
        out = rz.mostrar(self.T1, int(v.t0), 3)
        self.assertIn("<pre>%s</pre>" % ("z" * 300), out)
        self.assertIn("🧠 <b>CoT 1</b>", out)  # rótulo local reiniciado

    async def test_el_texto_de_cot_stream_no_se_manda(self):
        v = self._view(_FakeC(list(_FASES)),
                       cfg=_cfg(self.zt, cot_botones=True, cot_stream=True))
        v.run()
        self.assertFalse(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any("análisis terminado" in s[0] for s in self.sends))
        # cot_full queda ENTERO (nada se libera): las cajas lo cubren todo
        self.assertEqual("".join(v.cot_cajas),
                         "x" * 2000 + "y" * 1500 + "z" * 300)

    async def test_knob_apagado_comporta_como_antes(self):
        v = self._view(_FakeC(list(_FASES)),
                       cfg=_cfg(self.zt, cot_botones=False, cot_stream=True))
        v.run()
        # el CoT viaja como texto (cajas permanentes) y no hay botones
        self.assertTrue(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any(s[1] for s in self.sends))

    async def test_razonada_menos_del_tope_una_caja_al_cerrar(self):
        v = self._view(_FakeC([_ev(1, "reasoning_delta", "corto"),
                               _ev(2, "text_delta", "resp")]))
        v.run()
        self.assertEqual(v.cot_cajas, ["corto"])
        self.assertEqual(v.cot_rotulos, ["CoT 1"])
        final = [e for e in self.edits if e[2] and "Razonamiento" in e[1]][-1]
        self.assertIn("1 caja", final[1])

    async def test_turno_sin_razonamiento_no_crea_caja(self):
        v = self._view(_FakeC([_ev(1, "text_delta", "directo")]))
        v.run()
        self.assertFalse(any(s[1] for s in self.sends))
        self.assertFalse(os.path.exists(rz.cache_path(self.T1, int(v.t0))))


if __name__ == "__main__":
    unittest.main()
