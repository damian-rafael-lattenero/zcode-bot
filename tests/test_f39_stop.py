"""F39 — /stop honesto: fase del worker y verificación propia del kill.

Incidente 20/10 11:18: /stop a los 50s de arrancar una tarea → el worker
estaba RESUMIENDO la sesión (~130s en esta conversación) y no mira el
marker hasta terminar → gracia de 4s vencida → kill -9 → cartel '💀 no
frenó en 4s (el reporte llega por el chequeo de huérfanos)'… y el reporte
nunca llegó: el worker aún no estaba en bot-active, el chequeo no lo ve.
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import workers as zc_workers  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.kills = []
        # el pid falso es "nuestro" y está vivo; el kill se registra
        support.parchea(self, zc_workers, "_run_turn_ok", lambda pid: True)
        support.parchea(self, zc_workers, "_proc_vivo", lambda pid: True)
        support.parchea(self, zc_workers, "_kill_runtime",
                        lambda pid: (self.kills.append(pid), True)[1])
        support.parchea(self, zc_workers, "_STOP_VERIFICA_S", 0.05)

    def _worker(self, hace_s=10):
        self.zt._workers().register(support.T1, {
            "titulo": "testing", "pid": support.PID_FALSO,
            "t0": time.time() - hace_s})
        # el registry estampa el pid del proceso que registra (este test)
        self.pid = self.zt._workers().get(support.T1).get("pid")


class TestStopEnArranque(_Base):
    def test_arrancando_no_se_mata_y_el_marker_queda(self):
        self._worker(hace_s=50)  # como el incidente: 50s, resumiendo
        resp = self.zt.bot_stop(support.T1)
        self.assertIn("Frenando", resp)
        self.assertIn("arrancando el runtime", resp)
        self.assertIn("sin correr tu prompt", resp)
        self.assertNotIn("kill -9", resp)
        self.assertEqual(self.kills, [])
        # el marker sigue: el worker lo ve al terminar de arrancar y
        # aborta solo ('frenado por /stop (antes de enviar)')
        self.assertTrue(self.zt._stop_requested(support.T1))

    def test_arranque_colgado_se_mata_igual(self):
        self._worker(hace_s=zc_workers._STOP_ARRANQUE_MAX_S + 60)
        support.parchea(self, zc_workers, "_proc_muerto", lambda pid: True)
        resp = self.zt.bot_stop(support.T1, grace=0.05)
        self.assertEqual(self.kills, [self.pid])
        self.assertIn("kill -9", resp)
        self.assertIn("✅ verificado: proceso muerto", resp)
        self.assertFalse(self.zt._stop_requested(support.T1))  # consumido


class TestStopEnTurno(_Base):
    def setUp(self):
        super().setUp()
        self._worker(hace_s=300)
        self.zt._active_turn_register(support.T1, "testing",
                                      time.time() - 300)

    def test_kill_verificado(self):
        support.parchea(self, zc_workers, "_proc_muerto", lambda pid: True)
        resp = self.zt.bot_stop(support.T1, grace=0.05)
        self.assertEqual(self.kills, [self.pid])
        self.assertIn("no respondió al stop en 0s: kill -9", resp)
        self.assertIn("✅ verificado: proceso muerto", resp)
        # en turno SÍ hay reporte posible: está en bot-active
        self.assertIn("chequeo de huérfanos", resp)
        self.assertNotIn("(el reporte llega", resp)  # el cartel viejo
        self.assertFalse(self.zt._stop_requested(support.T1))

    def test_kill_pero_sigue_vivo_lo_dice(self):
        support.parchea(self, zc_workers, "_proc_muerto", lambda pid: False)
        resp = self.zt.bot_stop(support.T1, grace=0.05)
        self.assertIn("⚠️ el proceso sigue vivo", resp)
        self.assertIn("/status", resp)

    def test_anota_el_stop_en_el_turno_registrado(self):
        # F47c: el turno en bot-active queda marcado stop=<epoch> — es lo
        # que el supervisor de huérfanos lee para decir «frenada» y no
        # «falló» cuando el kill -9 se lleva al worker sin su finally
        support.parchea(self, zc_workers, "_proc_muerto", lambda pid: True)
        antes = time.time()
        self.zt.bot_stop(support.T1, grace=0.05)
        ent = self.zt._store("bot-active.json").read()[support.T1]
        self.assertGreaterEqual(ent["stop"], antes)
        self.assertEqual(ent["titulo"], "testing")  # el resto, intacto


class TestProcMuerto(unittest.TestCase):
    """os.kill(pid, 0) da por vivo a un ZOMBIE: para el /stop ya murió."""

    def _ps(self, stat):
        def run(args, **kw):
            return subprocess.CompletedProcess(args, 0, stdout=stat + "\n",
                                               stderr="")
        return run

    def test_pid_inexistente(self):
        with mock.patch.object(zc_workers, "_proc_vivo", lambda pid: False):
            self.assertTrue(zc_workers._proc_muerto(support.PID_FALSO))

    def test_zombie_cuenta_como_muerto(self):
        with mock.patch.object(zc_workers, "_proc_vivo", lambda pid: True), \
                mock.patch.object(zc_workers.subprocess, "run", self._ps("Z+")):
            self.assertTrue(zc_workers._proc_muerto(support.PID_FALSO))

    def test_vivo_de_verdad(self):
        with mock.patch.object(zc_workers, "_proc_vivo", lambda pid: True), \
                mock.patch.object(zc_workers.subprocess, "run", self._ps("S+")):
            self.assertFalse(zc_workers._proc_muerto(support.PID_FALSO))


if __name__ == "__main__":
    unittest.main()
