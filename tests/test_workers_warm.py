# -*- coding: utf-8 -*-
"""F8a — worker caliente: pickup del ítem encolado SIN nuevo runtime,
salida limpia por marker de /stop, timeout con limpieza completa,
Mac-ocupada → re-encolar y salir, turno caliente fallido sigue esperando,
y knob en 0 = el comportamiento clásico de un proceso por turno.
Reusa la infra de fakes de test_prompt_worker (mismo contrato)."""
import threading
import time
import unittest
from unittest import mock

import support
from test_prompt_worker import _cfg, _FakeC, _ViewRapida

from zcode_bot import workers as zc_workers
from zcode_bot.utils import http as zc_http


class TestWorkerCaliente(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        self.entregas = []
        self.c = _FakeC()
        self.makes = []
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
                                      lambda cwd=None, **kw:
                                      (self.makes.append(cwd), self.c)[1]),
                                  "proto_err": __import__(
                                      "zcode_bot.proto",
                                      fromlist=["proto_err"]).proto_err})),
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

    async def test_pickup_caliente_sin_nuevo_runtime(self):
        # el mensaje #2 llega mientras el worker está caliente: lo toma el
        # MISMO runtime (un solo make, un solo resume, dos entregas)
        zc_workers._queue_put(self.T2, self.act, "segundo prompt")
        zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=3),
                                 self.act, "primer prompt")
        self.assertEqual(len(self.makes), 1)
        resumes = [m for m, _p in self.c.requests if m == "session/resume"]
        self.assertEqual(len(resumes), 1)
        self.assertEqual(len(self.entregas), 2)
        sends_params = [p for m, p in self.c.requests if m == "session/send"]
        self.assertIn("segundo prompt", sends_params[1]["content"])
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)

    async def test_marker_stop_durante_el_warm_sale_limpio(self):
        t = threading.Timer(0.3, zc_workers._stop_request, args=(self.T2,))
        t.start()
        self.addCleanup(t.join)
        zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=8),
                                 self.act, "primer prompt")
        self.assertEqual(len(self.entregas), 1)  # el warm no corrió turnos
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)
        self.assertFalse(self.zt._stop_requested(self.T2))  # marker consumido

    async def test_timeout_libera_todo(self):
        zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=1),
                                 self.act, "primer prompt")
        self.assertEqual(len(self.entregas), 1)
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)
        self.assertEqual(self.zt._queue_peek(self.T2), [])

    async def test_mac_ocupada_reencola_y_sale(self):
        # F17 en caliente: la Mac tomó la sesión mientras esperábamos —
        # el ítem VUELVE a la cola (nada se pierde) y el worker sale
        busy = {"v": False}

        def running():
            return {self.T2: {}} if busy["v"] else {}

        def _flip():
            busy["v"] = True
            zc_workers._queue_put(self.T2, self.act, "durante warm")

        t = threading.Timer(0.3, _flip)
        t.start()
        self.addCleanup(t.join)
        with mock.patch.object(zc_workers, "running_turns", running):
            zc_workers._queue_put(self.T2, self.act, "durante warm")
            zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=6),
                                     self.act, "primer prompt")
        self.assertEqual(len(self.zt._queue_peek(self.T2)), 1)
        self.assertIsNone(self.zt._workers().get(self.T2))
        self.assertTrue(self.c.closed)

    async def test_turno_caliente_fallido_sigue_esperando(self):
        fins = [{"event": "turn.completed"},
                {"event": "turn.failed"},
                {"event": "turn.completed"}]
        with mock.patch.object(zc_workers, "_turn_end_poll",
                               lambda *a, **k: fins.pop(0)):
            zc_workers._queue_put(self.T2, self.act, "va a fallar")
            zc_workers._queue_put(self.T2, self.act, "y este anda")
            zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=2),
                                     self.act, "primero")
        self.assertTrue(any("falló" in s for s in self.sends))
        self.assertEqual(len(self.entregas), 2)  # el 1º y el que anduvo
        self.assertIsNone(self.zt._workers().get(self.T2))

    async def test_knob_cero_comportamiento_clasico(self):
        # warm_keepalive_s=0: ni pickup ni espera — la cola queda dormida
        # (el drain no spawnea: _spawn_worker anulado) y el runtime muere
        zc_workers._queue_put(self.T2, self.act, "queda dormido")
        with mock.patch.object(zc_workers, "_spawn_worker",
                               return_value=None):
            zc_workers.prompt_worker(_cfg(self.zt), self.act, "unico")
        self.assertEqual(len(self.entregas), 1)
        self.assertEqual(len(self.zt._queue_peek(self.T2)), 1)
        self.assertEqual(len(self.makes), 1)
        self.assertTrue(self.c.closed)

    async def test_heartbeat_refresca_y_marca(self):
        w = self.zt._workers()
        w.register(self.T2, {"t0": time.time(), "pid": 1})
        try:
            self.assertTrue(w.heartbeat(self.T2))
            ent = w.get(self.T2)
            self.assertTrue(ent.get("waiting"))
            self.assertFalse(w.heartbeat("sess_inexistente"))
        finally:
            w.pop(self.T2)

    async def test_pop_ajeno_no_se_lleva_la_entrada_del_vivo(self):
        # incidente 15:40: el atexit del worker DUPLICADO borraba la
        # entrada del worker caliente vivo — /stop y los guards ciegos
        import os
        w = self.zt._workers()

        def ajena(q):  # register pisa el pid: escribir el store directo
            # (pid 1 = launchd, vivo: si fuera muerto el _prune lo purga)
            q = dict(q) if isinstance(q, dict) else {}
            q[self.T2] = {"t0": time.time(), "pid": 1}
            return q
        w._store.update_cross_process(ajena, {})
        self.assertFalse(w.pop_if_mine(self.T2))  # ajena: intacta
        self.assertIsNotNone(w.get(self.T2))
        w.pop(self.T2)
        w.register(self.T2, {"t0": time.time()})
        self.assertTrue(w.pop_if_mine(self.T2))  # propia: fuera
        self.assertIsNone(w.get(self.T2))
        self.assertFalse(w.pop_if_mine("sess_inexistente"))


class TestRequeueOrdenFIFO(unittest.IsolatedAsyncioTestCase):
    """R0c — el requeue del warm PRESERVA el orden FIFO. La regresión
    latente: _warm_wait re-encolaba con reversed() + append (_queue_put
    hace append), que INVERTÍA la cola con ≥2 ítems — los mensajes del
    usuario habrían corrido al revés. Probado con 3."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}

    async def test_pickup_toma_el_primero_y_la_cola_queda_en_orden(self):
        for m in ("uno", "dos", "tres"):
            zc_workers._queue_put(self.T2, self.act, m)
        texto, act = zc_workers._warm_wait(_cfg(self.zt), self.T2, 5)
        self.assertEqual(texto, "uno")
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T2)],
                         ["dos", "tres"])

    async def test_task_ocupada_devuelve_todo_en_orden(self):
        with mock.patch.object(zc_workers, "running_turns",
                               lambda: {self.T2}):
            for m in ("uno", "dos", "tres"):
                zc_workers._queue_put(self.T2, self.act, m)
            self.assertIsNone(
                zc_workers._warm_wait(_cfg(self.zt), self.T2, 5))
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T2)],
                         ["uno", "dos", "tres"])


if __name__ == "__main__":
    unittest.main()
