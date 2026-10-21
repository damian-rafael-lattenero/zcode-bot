# -*- coding: utf-8 -*-
"""Happy-path of prompt_worker (F4g) — the bot's heart, which until
today had NO test of the complete flow (only the busy path had
coverage).

Freezes the end-to-end contract: runtime spawn → proto_ready →
session/send → live view → answer delivery → finally that cleans up
EVERYTHING (registry, bot-active, marker, runtime). With FakeProto +
the seams of the owning module (workers) — zero processes, zero
network, zero sleeps.
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
    """Fake ProtoClient: session ready, final answer available."""

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
    """Fake LiveTurnView: 0 loops, empty delivery."""

    def __init__(self, *a, **k):
        pass

    def run(self):
        return ("", "", [], 555)


class TestPromptWorkerFeliz(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2  # free and with a title in the fixture
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
        # 1) the runtime's answer reached the owner
        self.assertEqual(self.entregas, [("CV", "respuesta final del turno")])
        # 2) the worker's registry slot was left FREE (finally)
        self.assertIsNone(self.zt._workers().get(self.T2))
        # 3) the active turn was cleaned ({} remains — no registered turns)
        self.assertNotIn(self.T2, self.zt._store("bot-active.json").read() or {})
        # 4) the runtime was closed
        self.assertTrue(self.c.closed)
        # 5) the flow went through resume→setModel→setMode→send (proto_ready)
        metodos = [m for m, _p in self.c.requests]
        for esperado in ("session/resume", "session/setModel",
                         "session/setMode", "session/send"):
            self.assertIn(esperado, metodos)
        # 6) the prompt traveled with the headless guardrail appended (content field)
        send_params = [p for m, p in self.c.requests if m == "session/send"]
        self.assertTrue(send_params[0].get("content", "").endswith(
            self.zt.HEADLESS_GUARD))

    async def test_turno_fallido_avisa_y_no_entrega(self):
        with mock.patch.object(zc_workers, "_turn_end_poll",
                               lambda *a, **k: {"event": "turn.failed"}):
            zc_workers.prompt_worker(_cfg(self.zt), self.act, "hacé el CV")
        self.assertEqual(self.entregas, [])
        self.assertTrue(any("falló" in s for s in self.sends))
        self.assertIsNone(self.zt._workers().get(self.T2))  # still cleans up

    async def test_salta_los_sets_noop_arranque_rapido(self):
        # F7+: with the model already set (sqlite) and the mode registered,
        # startup does NOT pay setModel/setMode — each request returns the
        # full history (26MB ≈ 30s on large sessions, x3 per turn)
        from zcode_bot.models import MODEL_SEL
        with mock.patch.object(zc_workers, "_model_snapshot",
                               lambda sid: MODEL_SEL):
            zc_workers.prompt_worker(_cfg(self.zt),
                                     dict(self.act, mode_set="yolo"),
                                     "hacé el CV")
        metodos = [m for m, _p in self.c.requests]
        self.assertIn("session/resume", metodos)
        self.assertNotIn("session/setModel", metodos)
        self.assertNotIn("session/setMode", metodos)
        self.assertIn("session/send", metodos)


if __name__ == "__main__":
    unittest.main()
