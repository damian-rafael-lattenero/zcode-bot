# -*- coding: utf-8 -*-
"""Happy-path de prompt_worker (F4g) — el corazón del bot, hasta hoy SIN
ningún test del flujo completo (solo el path busy tenía cobertura).

Congela el contrato de punta a punta: spawn del runtime → proto_ready →
session/send → live view → entrega de la respuesta → finally que limpia
TODO (registry, bot-active, marker, runtime). Con FakeProto + los seams
del módulo dueño (workers) — cero procesos, cero red, cero sleeps.
"""
import json
import os
import unittest
from unittest import mock

import support

from zcode_bot import workers as zc_workers
from zcode_bot.utils import http as zc_http


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _FakeC:
    """ProtoClient de mentira: sesion lista, respuesta final disponible."""

    def __init__(self, respuesta="respuesta final del turno"):
        self.respuesta = respuesta
        self.requests = []
        self.closed = False

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        if method == "session/messages":
            return {"result": {"messages": [
                {"info": {"role": "user", "id": "m1"},
                 "parts": [{"type": "text", "text": "prompt"}]},
                {"info": {"role": "assistant", "id": "m2"},
                 "parts": [{"type": "text", "text": self.respuesta}]}]}}
        if method == "session/events":
            return {"result": {"events": []}}
        return {"result": {}}

    def close(self):
        self.closed = True


class _ViewRapida:
    """LiveTurnView de mentira: 0 vueltas, entrega vacía."""

    def __init__(self, *a, **k):
        pass

    def run(self):
        return ("", "", [], 555)


class TestPromptWorkerFeliz(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2  # libre y con título en el fixture
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        self.entregas = []
        self.c = _FakeC()
        patches = [
            mock.patch.object(zc_http, "send",
                              lambda cfg, text, loud=None, markup=None:
                              (self.sends.append(text), True)[1]),
            mock.patch.object(zc_http, "_live_send", lambda cfg, text: 555),
            mock.patch.object(zc_http, "_live_edit",
                              lambda cfg, mid, text: True),
            mock.patch.object(zc_workers, "zc_proto",
                              type("M", (), {
                                  "make": staticmethod(
                                      lambda cwd=None, **kw: self.c),
                                  "proto_err": __import__(
                                      "zcode_bot.proto", fromlist=["proto_err"]
                                  ).proto_err})),
            mock.patch.object(zc_workers, "_turn_end_poll",
                              lambda *a, **k: {"event": "turn.completed"}),
            mock.patch.object(zc_workers, "LiveTurnView", _ViewRapida),
            mock.patch.object(zc_workers, "_send_long_reply",
                              lambda cfg, tit, dur, texto, max_partes=3:
                              self.entregas.append((tit, texto))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_entrega_la_respuesta_y_limpia_todo(self):
        zc_workers.prompt_worker(_cfg(self.zt), self.act, "hacé el CV")
        # 1) la respuesta del runtime llegó al dueño
        self.assertEqual(self.entregas, [("CV", "respuesta final del turno")])
        # 2) el registro del worker quedó LIBRE (finally)
        self.assertIsNone(self.zt._workers().get(self.T2))
        # 3) el turno activo se limpió (queda {} — sin turnos registrados)
        self.assertNotIn(self.T2, self.zt._store("bot-active.json").read() or {})
        # 4) el runtime se cerró
        self.assertTrue(self.c.closed)
        # 5) el flujo pasó por resume→setModel→setMode→send (proto_ready)
        metodos = [m for m, _p in self.c.requests]
        for esperado in ("session/resume", "session/setModel",
                         "session/setMode", "session/send"):
            self.assertIn(esperado, metodos)
        # 6) el prompt viajó con el guardrail headless apendado (campo content)
        send_params = [p for m, p in self.c.requests if m == "session/send"]
        self.assertTrue(send_params[0].get("content", "").endswith(
            self.zt.HEADLESS_GUARD))

    async def test_turno_fallido_avisa_y_no_entrega(self):
        with mock.patch.object(zc_workers, "_turn_end_poll",
                               lambda *a, **k: {"event": "turn.failed"}):
            zc_workers.prompt_worker(_cfg(self.zt), self.act, "hacé el CV")
        self.assertEqual(self.entregas, [])
        self.assertTrue(any("falló" in s for s in self.sends))
        self.assertIsNone(self.zt._workers().get(self.T2))  # igual limpia


if __name__ == "__main__":
    unittest.main()
