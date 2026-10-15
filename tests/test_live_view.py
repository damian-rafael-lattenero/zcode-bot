# -*- coding: utf-8 -*-
"""Batería de LiveTurnView (F4d) — la vista en vivo del turno.

Antes eran 242 líneas con 2 tests (solo la pausa F15). Acá se congela el
resto del contrato: streaming de deltas con dedupe por eventId, flush de
CoT con mudanza de la caja viva, guardas anti-OOM (frame gigante, trip
de replay), /stop UNA sola vez, cierre sin pace y cadencia adaptativa.

Todo con deps inyectadas: cero red, cero time.sleep parcheado.
"""
import unittest

import support

from zcode_bot.live_view import (LiveTurnView, COT_TAIL, EVENTS_TRIP,
                                 LIVE_BIG_FRAME, LIVE_FRAME_TRIP)


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _FakeC:
    """ProtoClient de mentira: reenvía el replay COMPLETO en cada poll
    (el comportamiento real del server — así se ejercita el dedupe)."""

    def __init__(self, evs=None, max_frame=0):
        self.evs = evs or []
        self.max_frame = max_frame
        self.requests = []

    def request(self, method, params=None, timeout=None):
        self.requests.append(method)
        return {"result": {"events": [dict(e) for e in self.evs]}}


class _Th:
    """Thread que dura `n` vueltas del loop."""

    def __init__(self, n=2):
        self.n = n

    def is_alive(self):
        self.n -= 1
        return self.n > 0


def _ev(eid, kind, delta):
    return {"eventId": eid, "payload": {"kind": kind, "delta": delta}}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def _view(self, c, th=None, cfg=None, **dep_extra):
        """Vista con recorders: sends, edits, live_sends, skips, stops."""
        self.sends = []
        self.edits = []
        self.live_sends = []
        self.skips = []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append(text), True)[1],
            "live_send": lambda cfg, text:
                (self.live_sends.append(text), 550 + len(self.live_sends))[1],
            "live_edit": lambda cfg, mid, text:
                (self.edits.append((mid, text)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: self.skips.append(sid),
        }
        deps.update(dep_extra)
        import time as _t
        return LiveTurnView(c, cfg or _cfg(self.zt), self.T1, _t.time(),
                            "testing", 777, th or _Th(2), last_id=None,
                            deps=deps)


class TestStreaming(_Base):
    async def test_deltas_editan_la_caja_y_el_evento_de_fase(self):
        evs = [_ev(1, "reasoning_delta", "voy a pensar "),
               _ev(2, "text_delta", "escribo la respuesta")]
        v = self._view(_FakeC(evs))
        cot, texto, pasos, live_id = v.run()
        # el evento de fase pensar→escribir mandó su mensaje propio
        self.assertTrue(any("análisis terminado" in s for s in self.sends))
        # la caja viva edita con la fase de escritura
        self.assertTrue(any("escribiendo" in t for _m, t in self.edits))
        # el dedupe por eventId funciona: el replay reenvió 2 veces pero
        # los contadores ven cada delta UNA vez
        self.assertEqual(v.n_deltas, 2)
        self.assertTrue(texto.endswith("escribo la respuesta"))
        self.assertEqual(pasos, [])
        # el CIERRE libera TODO el CoT restante (aunque sea chico) como
        # mensaje permanente y la caja viva se muda — contrato del flush
        self.assertTrue(any("CoT 1" in s for s in self.sends))
        self.assertNotEqual(live_id, 777)
        self.assertEqual(cot, "")  # entregado al chat = liberado

    async def test_pasos_se_acumulan_con_caja_propia(self):
        msgs = [{"info": {"role": "assistant", "id": "m9"}, "parts": [
            {"type": "tool", "tool": "Bash",
             "state": {"input": {"command": ["ls"]}}}]}]
        v = self._view(_FakeC(), fetch_messages=lambda *a, **k: msgs)
        cot, texto, pasos, live_id = v.run()
        self.assertEqual([p for p in pasos], [("Bash", "ls")])
        # la caja de pasos nació (live_send) y se editó con el conteo
        self.assertTrue(any("pasos:" in s for s in self.live_sends))
        self.assertTrue(any("1 pasos" in t for _m, t in self.edits))


class TestCotFlush(_Base):
    async def test_flush_muda_la_caja_viva_y_el_cierre_manda_el_resto(self):
        evs = [_ev(1, "reasoning_delta", "R" * 5000)]
        v = self._view(_FakeC(evs))
        cot, _texto, _pasos, live_id = v.run()
        # CoT 1 (3000 chars) salió como mensaje permanente en el loop…
        self.assertTrue(any("CoT 1" in s for s in self.sends))
        # …la caja viva se MUDÓ al final (live_send nuevo id ≠ 777)…
        self.assertNotEqual(live_id, 777)
        # …y el cierre sin pace mandó el resto como CoT 2
        self.assertTrue(any("CoT 2" in s for s in self.sends))
        # lo entregado al chat se LIBERÓ: nada queda retenido en memoria
        self.assertEqual(cot, "")

    async def test_cot_stream_apagado_no_manda_nada(self):
        evs = [_ev(1, "reasoning_delta", "R" * 5000)]
        v = self._view(_FakeC(evs), cfg=_cfg(self.zt, cot_stream=False))
        cot, _t, _p, live_id = v.run()
        self.assertEqual(self.sends, [])
        self.assertEqual(live_id, 777)  # nunca se mudó
        self.assertEqual(len(cot), 5000)  # el CoT queda para el trace


class TestAntiOOM(_Base):
    async def test_frame_gigante_apaga_el_stream_y_marca_la_sesion(self):
        c = _FakeC(max_frame=LIVE_FRAME_TRIP + 1)
        v = self._view(c, th=_Th(3))
        v.run()
        self.assertTrue(v.events_off)
        self.assertEqual(self.skips, [self.T1])  # flag para los próximos
        # apagado = no volver a pedir el replay (la 3era vuelta no pidió)
        self.assertEqual(c.requests.count("session/events"), 1)

    async def test_trip_de_replay_apaga_sin_marcar(self):
        # 1300 eventos chicos: superan EVENTS_TRIP pero el frame es chico
        evs = [_ev(i, "reasoning_delta", "x") for i in range(EVENTS_TRIP + 100)]
        c = _FakeC(evs)
        v = self._view(c, th=_Th(2))
        v.run()
        self.assertTrue(v.events_off)
        self.assertEqual(self.skips, [])  # no es la sesión: es este turno

    async def test_cadencia_adaptativa_por_frame_grande(self):
        v = self._view(_FakeC(max_frame=LIVE_BIG_FRAME + 1), th=_Th(2))
        v.run()
        self.assertEqual(v.cad, 5.0)  # 2s normal, 5s con frame >2MB


class TestStop(_Base):
    async def test_stop_pide_session_stop_UNA_vez(self):
        c = _FakeC()
        v = self._view(c, th=_Th(4),
                       stop_requested=lambda sid: True)
        v.run()
        self.assertEqual(c.requests.count("session/stop"), 1)


if __name__ == "__main__":
    unittest.main()
