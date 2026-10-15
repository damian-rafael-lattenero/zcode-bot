# -*- coding: utf-8 -*-
"""F8b — rotación de sesión: transcripto sobre umbral → compact + turno
de resumen + sesión nueva sembrada (swap de bot-active, rekey de registry
y cola, restore del modelo de la vieja, aviso al chat). Debajo del
umbral no pasa nada; fallo de create/seed → continuidad en la sesión
vieja; /stop durante la danza → aborta sin tocar nada."""
import unittest
from unittest import mock

import support
from test_prompt_worker import _cfg, _FakeC, _ViewRapida

from zcode_bot import workers as zc_workers
from zcode_bot.utils import http as zc_http

SID2 = "sess_rotada-1111"


class _FakeCrea(_FakeC):
    """Runtime nuevo que sabe crear sesión (la rotación)."""

    def __init__(self, sid_nuevo=SID2):
        super().__init__()
        self.sid_nuevo = sid_nuevo

    def request(self, method, params=None, timeout=None):
        if method == "session/create":
            self.requests.append((method, params))
            return {"result": {"session": {"sessionId": self.sid_nuevo}}}
        return super().request(method, params, timeout)


class _CreaRota(_FakeC):
    """Runtime nuevo al que se le niega la sesión (fallo de create)."""

    def request(self, method, params=None, timeout=None):
        if method == "session/create":
            return {"error": {"code": -32000, "message": "boom"}}
        return super().request(method, params, timeout)


class TestRotacion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        self.entregas = []
        self.cs = []
        self.mbs = {"v": 20.0}
        self.restaurados = []
        self.crea_cls = _FakeCrea

        def make(cwd=None, **kw):
            c = _FakeC() if not self.cs else self.crea_cls()
            self.cs.append(c)
            return c

        def fetch(c, sid, after_id=None, limit=40):
            return [{"info": {"role": "assistant", "id": "m2"},
                     "parts": [{"type": "text",
                                "text": "ESTADO: resumen de continuidad"}]}]

        patches = [
            mock.patch.object(zc_http, "send",
                              lambda cfg, text, loud=None, markup=None:
                              (self.sends.append(text), True)[1]),
            mock.patch.object(zc_http, "_live_send", lambda cfg, text: 555),
            mock.patch.object(zc_http, "_live_edit",
                              lambda cfg, mid, text: True),
            mock.patch.object(zc_workers, "zc_proto",
                              type("M", (), {
                                  "make": staticmethod(make),
                                  "proto_err": __import__(
                                      "zcode_bot.proto",
                                      fromlist=["proto_err"]).proto_err})),
            mock.patch.object(zc_workers, "_turn_end_poll",
                              lambda *a, **k: {"event": "turn.completed"}),
            mock.patch.object(zc_workers, "_wait_cli_event",
                              lambda *a, **k: {"event": "compact.completed"}),
            mock.patch.object(zc_workers, "_fetch_messages", fetch),
            mock.patch.object(zc_workers, "_transcript_mb",
                              lambda sid: self.mbs["v"]),
            mock.patch.object(zc_workers, "_restore_model",
                              lambda sid, snap:
                              self.restaurados.append((sid, snap))),
            mock.patch.object(zc_workers, "LiveTurnView", _ViewRapida),
            mock.patch.object(zc_workers, "_send_long_reply",
                              lambda cfg, tit, dur, texto, max_partes=3:
                              self.entregas.append((tit, texto))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_rota_cuando_supera_el_umbral(self):
        zc_workers.prompt_worker(
            _cfg(self.zt, warm_keepalive_s=1, rotar_umbral_mb=8),
            self.act, "hola")
        # la danza completa: compact y turno de resumen sobre la vieja,
        # seed sobre la nueva
        met_viejo = [m for m, _p in self.cs[0].requests]
        self.assertIn("session/compact", met_viejo)
        self.assertTrue(any("Escribí el resumen de continuidad"
                            in p.get("content", "")
                            for m, p in self.cs[0].requests
                            if m == "session/send"))
        met_nuevo = [m for m, _p in self.cs[1].requests]
        self.assertIn("session/create", met_nuevo)
        seed_params = [p for m, p in self.cs[1].requests
                       if m == "session/send"]
        self.assertTrue(any("resumen de continuidad" in p.get("content", "")
                            for p in seed_params))
        # swap: la sesión activa es la nueva
        self.assertEqual(zc_workers.active_bot_session().get("sid"), SID2)
        # la vieja quedó restaurada con SU snapshot y su runtime cerrado
        self.assertEqual(len(self.restaurados), 1)
        self.assertEqual(self.restaurados[0][0], self.T2)
        self.assertTrue(self.cs[0].closed)
        # aviso al chat
        self.assertTrue(any("rotada" in s for s in self.sends))

    async def test_bajo_el_umbral_no_rota(self):
        self.mbs["v"] = 1.0
        zc_workers.prompt_worker(
            _cfg(self.zt, warm_keepalive_s=1, rotar_umbral_mb=8),
            self.act, "hola")
        self.assertEqual(len(self.cs), 1)
        self.assertNotIn("session/compact",
                         [m for m, _p in self.cs[0].requests])
        self.assertFalse(any("♻️" in s for s in self.sends))
        self.assertEqual(zc_workers.active_bot_session().get("sid"),
                         self.T2)

    async def test_fallo_de_create_sigue_en_la_vieja(self):
        self.crea_cls = _CreaRota
        zc_workers.prompt_worker(
            _cfg(self.zt, warm_keepalive_s=1, rotar_umbral_mb=8),
            self.act, "hola")
        # intentó (2º runtime) pero volvió: sin swap, aviso, y el runtime
        # descartado quedó cerrado
        self.assertEqual(len(self.cs), 2)
        self.assertTrue(self.cs[1].closed)
        self.assertEqual(zc_workers.active_bot_session().get("sid"),
                         self.T2)
        self.assertFalse(any("rotada:" in s for s in self.sends))
        self.assertTrue(any("No pude crear" in s for s in self.sends))
        self.assertTrue(self.cs[0].closed)  # el warm igual terminó limpio

    async def test_stop_durante_la_danza_no_toca_nada(self):
        c = _FakeC()
        zc_workers._stop_request(self.T2)
        try:
            res = zc_workers._rotar_sesion(
                c, _cfg(self.zt), self.act, self.T2, support.PROJ_CV,
                "CV", None, None)
            self.assertIsNone(res)
            self.assertNotIn("session/compact",
                             [m for m, _p in c.requests])
        finally:
            zc_workers._stop_consume(self.T2)

    async def test_queue_rekey_mueve_los_encolados(self):
        zc_workers._queue_put(self.T2, self.act, "m1")
        zc_workers._queue_rekey(self.T2, SID2)
        self.assertEqual(self.zt._queue_peek(self.T2), [])
        self.assertEqual(len(self.zt._queue_peek(SID2)), 1)
        zc_workers._queue_rekey(SID2, SID2)  # noop sin crash
        zc_workers._queue_rekey(SID2, None)  # noop sin crash


if __name__ == "__main__":
    unittest.main()
