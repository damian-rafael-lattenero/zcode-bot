"""F17 — cola explícita para "la Mac está ocupada" (pedido de Damian
2026-10-13: "que el mensaje se encole y me diga que la Mac está ocupada
y que mi mensaje está en la cola, y /stop debería limpiar la cola").

Contexto del incidente que motivó el pedido: con la task ocupada por la
corrida de la Mac, cada mensaje del celular spawnneaba un runtime de
~500MB para descubrir "busy", encolaba, y el drain del worker lo
relanzaba en LOOP (5 workers en 5 s en pwlog real).
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

import support
from zcode_bot import workers as zc_workers        # módulo dueño (F4g)
from zcode_bot.utils import http as zc_http


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1  # activa y CORRIENDO en el fixture
        self.T2 = support.T2  # completada en el fixture
        self.act1 = {"sid": self.T1, "title": "testing", "ws": support.PROJ_BOT}
        self.act2 = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        # F4g: prompt_worker vive en workers y usa uhttp.* — el transporte
        # se stubbea en el seam compartido (gateway sigue con su binding
        # para handle_free_text, aún no migrado)
        self.zt.send = lambda cfg, text, loud=None, markup=None: \
            (self.sends.append(text), True)[1]
        for name, fn in (("send", lambda cfg, text, loud=None, markup=None:
                          (self.sends.append(text), True)[1]),
                         ("_live_send", lambda cfg, text: 555),
                         ("_live_edit", lambda cfg, mid, text: True)):
            pp = mock.patch.object(zc_http, name, fn)
            pp.start()
            self.addCleanup(pp.stop)
        self.spawns = []

        def spy_spawn(cfg, act, text):
            self.spawns.append((act.get("sid"), text))
            return object()
        # el spy cubre AMBOS consumidores: workers (_queue_launch_next/
        # _queue_resume) y gateway (handle_free_text)
        self.zt._spawn_worker = spy_spawn
        pw = mock.patch.object(zc_workers, "_spawn_worker", spy_spawn)
        pw.start()
        self.addCleanup(pw.stop)


class TestBusyInline(_Base):
    """Texto libre con la task ocupada: encola y AVISA — sin spawn."""

    async def test_ocupada_encola_y_avisa(self):
        zt = self.zt
        self.assertIn(self.T1, zt.running_turns())  # precondition del fixture
        resp = zt.handle_free_text(_cfg(zt), "seguir con el bug")
        self.assertIn("ocupada", resp)
        self.assertIn("cola", resp)
        self.assertEqual(self.spawns, [])          # NI un proceso lanzado
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)

    async def test_mensajes_sucesivos_acumulan_y_posicionan(self):
        zt = self.zt
        r1 = zt.handle_free_text(_cfg(zt), "uno")
        r2 = zt.handle_free_text(_cfg(zt), "dos")
        self.assertIn("1 en espera", r1)
        self.assertIn("2 en espera", r2)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T1)],
                         ["uno", "dos"])

    async def test_libera_si_la_task_no_corre(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)  # T2 completada en el fixture
        resp = zt.handle_free_text(_cfg(zt), "seguir el CV")
        self.assertIn("trabajando", resp)
        self.assertEqual(self.spawns, [(self.T2, "seguir el CV")])

    async def test_worker_tampoco_spawnea_runtime_si_busy(self):
        # defensa en profundidad: /run o relays que lleguen directo al
        # worker tampoco abren runtime para descubrir "busy"
        zt = self.zt
        zc_workers.zc_proto = types.SimpleNamespace(
            make=lambda **kw: (_ for _ in ()).throw(AssertionError(
                "no debe spawnnear runtime con la task ocupada")))
        zt.prompt_worker(_cfg(zt), self.act1, "mensaje")
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)
        self.assertTrue(any("ocupada" in s for s in self.sends))
        self.assertIsNone(zt._workers().get(self.T1))  # no quedó registrada


class TestDrainNoLoopea(_Base):
    """El relay de cola DUERME mientras la sesión sigue ocupada (regresión
    del loop de 18:03)."""

    async def test_drain_dormido_si_ocupada(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        zt._queue_drain(_cfg(zt), self.T1)
        self.assertEqual(self.spawns, [])
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)  # intacta

    async def test_drain_relay_si_libre_y_current(self):
        zt = self.zt
        zt._queue_put(self.T2, self.act2, "uno")
        zt.set_active_bot_session(self.act2)
        zt._queue_drain(_cfg(zt), self.T2)
        self.assertEqual(self.spawns, [(self.T2, "uno")])


class TestOnStopDespiertaCola(_Base):
    """La Mac terminó su turno → la cola de la task current corre sola."""

    def _p(self, sid):
        return {"sessionId": sid, "responsePreview": "listo"}

    async def test_resume_al_terminar_la_mac(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "mensaje diferido")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [(self.T1, "mensaje diferido")])
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertTrue(any("Retomé" in s for s in self.sends))

    async def test_con_worker_del_bot_no_duplica(self):
        # el drain del worker es el dueño del relay: on_stop no compite
        zt = self.zt
        zt._workers().register(self.T1, {"titulo": "testing"})
        zt._queue_put(self.T1, self.act1, "x")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [])
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)  # el drain decide

    async def test_no_current_no_resume(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)
        zt._queue_put(self.T1, self.act1, "x")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [])  # F15: duerme hasta /task


class TestStopLimpiaCola(_Base):
    """/stop frena Y limpia la cola de esa task."""

    async def test_stop_con_worker_limpia(self):
        zt = self.zt
        zt._workers().register(self.T1, {"titulo": "testing"})
        zt._queue_put(self.T1, self.act1, "uno")
        zt._queue_put(self.T1, self.act1, "dos")
        resp = zt.bot_stop(self.T1)
        self.assertIn("Frenando", resp)
        self.assertIn("cola limpiada (2)", resp)
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertTrue(zt._stop_requested(self.T1))  # marker del actor
        zt._stop_consume(self.T1)

    async def test_stop_sin_worker_solo_limpia(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        resp = zt.bot_stop(self.T1)
        self.assertIn("Limpié la cola", resp)
        self.assertIn("1", resp)
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertFalse(zt._stop_requested(self.T1))  # nada que frenar

    async def test_stop_sin_nada_como_antes(self):
        self.assertIn("ya no existe", self.zt.bot_stop(self.T1))

    async def test_lista_muestra_las_colas(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        rep = zt.stop_list_report()
        texto = rep if isinstance(rep, str) else rep[0]
        self.assertIn("Cola", texto)
        self.assertIn("1", texto)
        markup = rep[1] if isinstance(rep, tuple) else None
        flat = [b["callback_data"] for row in markup["inline_keyboard"]
                for b in row] if markup else []
        self.assertIn("s:%s" % self.T1, flat)  # botón que limpia


if __name__ == "__main__":
    unittest.main()
