"""F17 — explicit queue for "the Mac is busy" (Damian's request
2026-10-13: "queue the message and tell me the Mac is busy and that my
message is in the queue, and /stop should clear the queue").

Context of the incident that motivated the request: with the task busy
from the Mac's run, every phone message spawned a ~500MB runtime just
to discover "busy", queued it, and the worker's drain relaunched it in
a LOOP (5 workers in 5 s in a real pwlog).
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

import support
from zcode_bot import workers as zc_workers        # owning module (F4g)
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
        self.T1 = support.T1  # active and RUNNING in the fixture
        self.T2 = support.T2  # completed in the fixture
        self.act1 = {"sid": self.T1, "title": "testing", "ws": support.PROJ_BOT}
        self.act2 = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        # F4g: prompt_worker lives in workers and uses uhttp.* — the
        # transport is stubbed at the shared seam (the gateway keeps its
        # own binding for handle_free_text, not yet migrated)
        support.parchea(self, self.zt, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), True)[1])
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
        # the spy covers ALL spawn consumers: workers
        # (_queue_launch_next/_queue_resume) and commands.dispatch
        # (handle_free_text)
        from zcode_bot.commands import handlers_tareas as zc_tareas
        support.parchea(self, self.zt, "_spawn_worker", spy_spawn)
        for mod in (zc_workers, zc_tareas):
            pw = mock.patch.object(mod, "_spawn_worker", spy_spawn)
            pw.start()
            self.addCleanup(pw.stop)


class TestBusyInline(_Base):
    """Free text while the task is busy: queues and NOTIFIES — no spawn."""

    async def test_ocupada_encola_y_avisa(self):
        zt = self.zt
        self.assertIn(self.T1, zt.running_turns())  # fixture precondition
        resp = zt.handle_free_text(_cfg(zt), "seguir con el bug")
        self.assertIn("ocupada", resp)
        self.assertIn("cola", resp)
        self.assertEqual(self.spawns, [])          # NOT one process launched
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
        zt.set_active_bot_session(self.act2)  # T2 completed in the fixture
        resp = zt.handle_free_text(_cfg(zt), "seguir el CV")
        self.assertIn("trabajando", resp)
        self.assertEqual(self.spawns, [(self.T2, "seguir el CV")])

    async def test_worker_tampoco_spawnea_runtime_si_busy(self):
        # defense in depth: /run or relays arriving directly at the
        # worker also do not open a runtime just to discover "busy"
        zt = self.zt
        support.parchea(self, zc_workers, "zc_proto", types.SimpleNamespace(
            make=lambda **kw: (_ for _ in ()).throw(AssertionError(
                "no debe spawnnear runtime con la task ocupada"))))
        zt.prompt_worker(_cfg(zt), self.act1, "mensaje")
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)
        self.assertTrue(any("ocupada" in s for s in self.sends))
        self.assertIsNone(zt._workers().get(self.T1))  # not left registered


class TestDrainNoLoopea(_Base):
    """The queue relay SLEEPS while the session stays busy (regression
    of the 18:03 loop)."""

    async def test_drain_dormido_si_ocupada(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        zt._queue_drain(_cfg(zt), self.T1)
        self.assertEqual(self.spawns, [])
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)  # intact

    async def test_drain_relay_si_libre_y_current(self):
        zt = self.zt
        zt._queue_put(self.T2, self.act2, "uno")
        zt.set_active_bot_session(self.act2)
        zt._queue_drain(_cfg(zt), self.T2)
        self.assertEqual(self.spawns, [(self.T2, "uno")])


class TestOnStopDespiertaCola(_Base):
    """The Mac finished its turn → the current task's queue runs on its own."""

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
        # the worker's drain owns the relay: on_stop does not compete
        zt = self.zt
        zt._workers().register(self.T1, {"titulo": "testing"})
        zt._queue_put(self.T1, self.act1, "x")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [])
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)  # the drain decides

    async def test_no_current_no_resume(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)
        zt._queue_put(self.T1, self.act1, "x")
        zt.on_stop(_cfg(zt), self._p(self.T1))
        self.assertEqual(self.spawns, [])  # F15: sleeps until /task


class TestStopLimpiaCola(_Base):
    """/stop halts AND clears that task's queue."""

    async def test_stop_con_worker_limpia(self):
        zt = self.zt
        zt._workers().register(self.T1, {"titulo": "testing"})
        zt._queue_put(self.T1, self.act1, "uno")
        zt._queue_put(self.T1, self.act1, "dos")
        resp = zt.bot_stop(self.T1)
        self.assertIn("Frenando", resp)
        self.assertIn("cola limpiada (2)", resp)
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertTrue(zt._stop_requested(self.T1))  # actor's marker
        zt._stop_consume(self.T1)

    async def test_stop_sin_worker_solo_limpia(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        resp = zt.bot_stop(self.T1)
        self.assertIn("Limpié la cola", resp)
        self.assertIn("1", resp)
        self.assertEqual(zt._queue_peek(self.T1), [])
        self.assertFalse(zt._stop_requested(self.T1))  # nothing to halt

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
        self.assertIn("s:%s" % self.T1, flat)  # button that clears


class TestQgoQdel(_Base):
    """/qgo and /qdel against the REAL queue (refactor regression R0a:
    F34b used `config.ARCH_COLA` in _queue_mutate without importing the
    module — a NameError that left both commands broken since 19/10
    12:55, with the suite green because no test exercised them for
    real)."""

    async def test_qdel_descarta_de_la_cola_real(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        zt._queue_put(self.T1, self.act1, "dos")
        resp = zt._cmd_qdel(_cfg(zt), "%s:0" % self.T1)
        self.assertIn("🗑 Descartado: uno", resp)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T1)],
                         ["dos"])

    async def test_qdel_indice_viejo_no_descarta_nada(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        resp = zt._cmd_qdel(_cfg(zt), "%s:5" % self.T1)
        self.assertIn("ya no está", resp)
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)

    async def test_qgo_lanza_yo_saca_de_la_cola(self):
        # T2 is COMPLETED in the fixture: passes the busy guard
        zt = self.zt
        zt._queue_put(self.T2, self.act2, "uno")
        zt._queue_put(self.T2, self.act2, "dos")
        resp = zt._cmd_qgo(_cfg(zt), "%s:1" % self.T2)
        self.assertIn("⏫ Lanzado: dos", resp)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T2)],
                         ["uno"])
        self.assertEqual(self.spawns[-1], (self.T2, "dos"))

    async def test_qgo_con_la_sesion_ocupada_no_lo_saca(self):
        zt = self.zt
        zt._queue_put(self.T1, self.act1, "uno")
        resp = zt._cmd_qgo(_cfg(zt), "%s:0" % self.T1)
        self.assertIn("sigue ocupada", resp)
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)


if __name__ == "__main__":
    unittest.main()

    async def test_cola_llena_avisa_cual_cayo(self):
        # F51b: on the 6th enqueue the oldest was dropped IN SILENCE
        zt = self.zt
        sends = []
        with mock.patch.object(zc_http, "send",
                               lambda cfg, text, loud=None, markup=None:
                               sends.append(text) or True):
            cfg = _cfg(zt)
            for i in range(5):
                zt._queue_put(self.T1, self.act1, "msj %d" % i, cfg=cfg)
            self.assertEqual(sends, [])  # up to here everything fits
            zt._queue_put(self.T1, self.act1, "el sexto", cfg=cfg)
        self.assertEqual(len(sends), 1)
        self.assertIn("Cola llena (5)", sends[0])
        self.assertIn("msj 0", sends[0])
        # the LAST 5 remain (FIFO: the oldest was the one dropped)
        self.assertEqual([i["text"] for i in zt._queue_peek(self.T1)],
                         ["msj 1", "msj 2", "msj 3", "msj 4", "el sexto"])

    async def test_cola_llena_sin_cfg_no_intenta_avisar(self):
        # internal re-enqueues (requeue) have no cfg: no notice, no
        # blow-up — the drop is only recorded in the future pwlog
        zt = self.zt
        for i in range(6):
            self.assertTrue(zt._queue_put(self.T1, self.act1, "m%d" % i))
        self.assertEqual(len(zt._queue_peek(self.T1)), 5)
