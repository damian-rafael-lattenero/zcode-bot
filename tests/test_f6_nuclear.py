"""F6: /nuclear — reset de emergencia.

Congela el contrato: confirmación en dos pasos (solo 'go' ejecuta — y 'go'
solo lo produce el botón), callbacks nuc:*, ciclo de reinicio de la app con
procesos y reloj inyectados, y presencia en menú/help.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import support


class FakeProc:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.returncode = 0


class TestCallbacksNuclear(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_botones_de_confirmacion(self):
        self.assertEqual(self.zt._cb_text("nuc:go"), "/nuclear go")
        self.assertEqual(self.zt._cb_text("nuc:no"), "/nuclear no")


class TestComandoNuclear(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_sin_args_pide_confirmacion_por_boton(self):
        r = self.zt._cmd_nuclear({}, "")
        self.assertIsInstance(r, tuple)
        texto, markup = r
        self.assertIn("¿Confirmás?", texto)
        cbs = {b["callback_data"] for b in markup["inline_keyboard"][0]}
        self.assertEqual(cbs, {"nuc:go", "nuc:no"})

    def test_bare_nuclear_por_texto_solo_confirma(self):
        # tipear "nuclear" (alias bare) muestra la confirmación: NUNCA ejecuta
        r = self.zt.handle_command({}, "nuclear")
        self.assertIsInstance(r, tuple)
        self.assertIn("¿Confirmás?", r[0])

    def test_alias_bare_con_args_no_es_comando(self):
        self.assertIsNone(self.zt.handle_command({}, "nuke fiesta"))

    def test_no_cancela(self):
        self.assertIn("Cancelado", self.zt._cmd_nuclear({}, "no"))

    def test_go_arranca_el_nuclear_en_un_thread(self):
        corrio = []
        self.zt._nuclear_run = lambda cfg: corrio.append(cfg)
        r = self.zt._cmd_nuclear({"x": 1}, "go")
        self.assertIn("Nuclear 2.0", r)
        for _ in range(200):
            if corrio:
                break
            time.sleep(0.01)
        self.assertEqual(corrio, [{"x": 1}])


class TestAppRestartCycle(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    @staticmethod
    def _mkclock():
        clock = [0.0]

        def sleep(s):
            clock[0] += max(s, 1.0)

        return clock, sleep, (lambda: clock[0])

    def test_ciclo_feliz(self):
        calls = []
        state = {"app": True, "host": False}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            return [999] if state["host"] else []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["had_app"])
        self.assertTrue(rep["app_ok"])
        self.assertEqual(rep["notes"], [])
        self.assertEqual([c[:2] for c in calls],
                         [["killall", "-TERM"], ["open", "-a"]])  # ni KILL ni pkill

    def test_app_rebelde_sigkill_y_nota(self):
        calls = []
        state = {"app": True, "host": False}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-KILL"]:
                state["app"] = False
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertIn("SIGKILL", rep["notes"][0])
        self.assertIn(["killall", "-KILL", "ZCode"], calls)

    def test_host_huerfano_se_termina_con_pkill(self):
        calls = []
        state = {"app": True, "host": True}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["pkill", "-TERM"]:
                state["host"] = False
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            if "-P" in pgrep_args:
                return []
            return [999] if state["host"] else []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertTrue(any("host huérfano" in n for n in rep["notes"]))
        self.assertIn(["pkill", "-TERM", "-f", "zcode-host-local"], calls)

    def test_cli_huerfanos_se_cierran_antes_de_reabrir(self):
        calls = []
        state = {"app": True, "cli": [555, 556]}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["kill", "-TERM"]:
                state["cli"] = [p for p in state["cli"] if p != int(args[2])]
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            if "-P" in pgrep_args:
                return list(state["cli"])
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertEqual(rep["notes"], [])
        self.assertIn(["kill", "-TERM", "555"], calls)
        self.assertIn(["kill", "-TERM", "556"], calls)
        # la reapertura va DESPUÉS de las muertes de los cli
        self.assertLess(calls.index(["kill", "-TERM", "556"]),
                        calls.index(["open", "-a", "ZCode"]))

    def test_cli_rebelde_va_a_sigkill_con_nota(self):
        calls = []
        state = {"app": True, "cli": [555]}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["kill", "-KILL"]:
                state["cli"] = []
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            if "-P" in pgrep_args:
                return list(state["cli"])
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertTrue(any("SIGKILL" in n for n in rep["notes"]))
        self.assertIn(["kill", "-KILL", "555"], calls)

    def test_sin_app_solo_la_abre(self):
        calls = []
        state = {"app": False, "host": False}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertFalse(rep["had_app"])
        self.assertTrue(rep["app_ok"])
        self.assertEqual([c[:2] for c in calls], [["open", "-a"]])


class TestKillOwnRuntimes(unittest.TestCase):
    """F16: el remate duro va por REGISTRY (pids de workers-proceso),
    ya no por pgrep de hijos del poller (los runtimes son nietos)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_sigterm_a_los_pids_del_registry(self):
        import os as _os
        zt = self.zt
        zt.WORKERS.register("s1", {"titulo": "a", "pid": _os.getpid()})
        zt.WORKERS.register("s2", {"titulo": "b", "pid": _os.getpid()})
        # el propio pid se excluye (nunca hay un worker de ESTE proceso en
        # producción, pero el test usa getpid como pid conocido)
        senales = []
        real_kill = _os.kill

        def fake_kill(pid, sig):
            if sig == 15:  # SIGTERM — los SIGKILL (9) del remate no: el pid
                senales.append(pid)  # es el propio test y moriría
            return real_kill(pid, sig) if sig != 9 else None

        with mock.patch.object(zt.os, "kill", fake_kill), \
                mock.patch.object(zt.time, "sleep"):
            zt._nuc_kill_own_runtimes()
        self.assertEqual(senales, [])  # pids propios: excluidos

    def test_sigterm_a_pid_ajeno(self):
        zt = self.zt
        senales = []
        # pid de un proceso ajeno real y efímero (no mata a nadie del test)
        ajeno = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            zt.WORKERS.register("sX", {"titulo": "x", "pid": ajeno.pid})
            ajeno.kill()  # ya está muerto: el test no lo toca de verdad
            ajeno.wait()
            # un pid muerto: os.kill lanza ProcessLookupError → ignorado
            zt._nuc_kill_own_runtimes()
            senales.append("sin-crash")
            self.assertEqual(senales, ["sin-crash"])
        finally:
            try:
                ajeno.kill()
                ajeno.wait()
            except OSError:
                pass


class TestNuclearGuardiaRecarga(unittest.TestCase):
    def test_flag_activo_solo_durante_el_ciclo(self):
        zt = support.fresh(self)
        visto = []
        zt._nuclear_body = lambda cfg: visto.append(zt._nuclear_active())
        zt._cmd_nuclear({}, "go")
        for _ in range(200):
            if visto:
                break
            time.sleep(0.01)
        time.sleep(0.1)  # dejar correr el finally
        self.assertEqual(visto, [True])
        self.assertFalse(zt._nuclear_active())
        self.assertFalse(os.path.exists(zt._nuclear_flag_path()))

    def test_flag_huerfano_con_ttl_se_purga(self):
        # F21 — REGRESIÓN del incidente 18:56: un nuclear kickstea el
        # poller antes de su finally → flag huérfano → el gate de reload
        # bloqueó TODOS los deploys en silencio por horas. TTL 120s:
        # huérfano = ignorar Y limpiar.
        zt = support.fresh(self)
        p = zt._nuclear_flag_path()
        with open(p, "w") as f:
            f.write(str(time.time() - 3600))  # 1 hora: huérfano
        viejo = time.time() - 3600
        os.utime(p, (viejo, viejo))
        self.assertFalse(zt._nuclear_active())  # no bloquea
        self.assertFalse(os.path.exists(p))     # y se limpia solo

    def test_flag_fresco_bloquea(self):
        zt = support.fresh(self)
        with open(zt._nuclear_flag_path(), "w") as f:
            f.write(str(time.time()))
        self.assertTrue(zt._nuclear_active())


class TestNuclear20(unittest.TestCase):
    """Nuclear 2.0 — operación anunciada y verificada.

    1 pre-vuelo anuncia N corridas / M turnos Mac / K cola conservada
    2 cerradura: spawn/drain/resume bloqueados durante el ciclo
    3 frenado SIN limpiar la cola (semántica nueva)
    4 heartbeat: el flag vivo no se purga por viejo; sin heartbeat, sí
    5 watchdog: reporte final único o aviso de fracaso; flag siempre limpio
    """

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1
        self.sends = []
        self.zt.send = lambda cfg, text, loud=None, markup=None: \
            (self.sends.append(text), True)[1]

    # ---------- helpers ----------

    def _flag(self):
        return self.zt._nuclear_flag_path()

    def _con_flag(self):
        self.zt._nuclear_heartbeat()

    def _worker_falso(self, sid, pid=424242):
        self.zt.WORKERS.register(sid, {"pid": pid, "t0": time.time(),
                                       "titulo": "t", "prompt": "p",
                                       "ws": "/w"})

    # ---------- fase 0: pre-vuelo ----------

    def test_prevuelo_anuncia_n_m_k(self):
        zt = self.zt
        self._worker_falso(self.T1)
        zt.running_turns = lambda: {self.T1: time.time() - 10,
                                    "sess_otra": time.time() - 5}
        zt._queue_put(self.T1, {"sid": self.T1, "ws": "/w"}, "mensaje cola")
        zt._nuc_kill_own_runtimes = lambda: None
        zt.app_restart_cycle = lambda: {"had_app": True, "app_ok": True,
                                        "notes": []}
        zt._spawn_nuclear_watchdog = lambda: None
        zt.subprocess = mock.Mock()
        zt._nuclear_body({})
        prevuelo = self.sends[0]
        # 1 worker del bot (T1) + 1 turno de Mac sin worker (sess_otra)
        self.assertIn("1 corrida", prevuelo)
        self.assertIn("1 turno", prevuelo)
        self.assertIn("SE CONSERVAN", prevuelo)
        self.assertIn("1 mensaje", prevuelo)

    def test_frenado_conserva_la_cola(self):
        # regresión: bot_stop limpiaba la cola; el nuclear NO debe
        zt = self.zt
        self._worker_falso(self.T1)
        zt._queue_put(self.T1, {"sid": self.T1, "ws": "/w"}, "sobreviví")
        zt._nuc_kill_own_runtimes = lambda: None
        zt.app_restart_cycle = lambda: {"had_app": False, "app_ok": True,
                                        "notes": []}
        zt._spawn_nuclear_watchdog = lambda: None
        zt.subprocess = mock.Mock()
        zt._nuclear_body({})
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)
        self.assertTrue(zt.WORKERS.stopped(self.T1))  # marker puesto
        self.assertTrue(os.path.exists(zt._stop_file(self.T1)))

    def test_reporte_para_el_watchdog(self):
        zt = self.zt
        zt._nuc_kill_own_runtimes = lambda: None
        zt.app_restart_cycle = lambda: {"had_app": True, "app_ok": True,
                                        "notes": ["nota x"]}
        zt._spawn_nuclear_watchdog = lambda: None
        zt.subprocess = mock.Mock()
        zt._nuclear_body({})
        rep = zt._store("bot-nuclear-report.json").read()
        self.assertEqual(rep["frenadas"], 0)
        self.assertEqual(rep["notes"], ["nota x"])
        self.assertTrue(rep["app_ok"])

    # ---------- fase 1: cerradura ----------

    def test_spawn_bloqueado_durante_nuclear(self):
        zt = self.zt
        self._con_flag()
        self.assertIsNone(zt._spawn_worker({}, {"sid": self.T1}, "hola"))
        zt._nuclear_active()  # purga para no ensuciar otros tests

    def test_drain_y_resume_dormidos_durante_nuclear(self):
        zt = self.zt
        self._con_flag()
        zt._queue_put(self.T1, {"sid": self.T1, "ws": "/w"}, "m")
        zt.running_turns = lambda: {}
        zt._queue_launch_next = mock.Mock(return_value="x")
        zt._queue_drain({}, self.T1)
        zt._queue_resume({}, self.T1)
        zt._queue_launch_next.assert_not_called()
        zt._nuclear_active()  # purge

    # ---------- fase 3: heartbeat ----------

    def test_heartbeat_mantiene_el_flag_vivo(self):
        zt = self.zt
        self._con_flag()
        self.assertTrue(zt._nuclear_active())  # heartbeateado hace 0s
        zt._nuclear_heartbeat()
        self.assertTrue(zt._nuclear_active())
        os.remove(self._flag())

    def test_sin_heartbeat_se_purga(self):
        zt = self.zt
        self._con_flag()
        viejo = time.time() - 3600
        os.utime(self._flag(), (viejo, viejo))  # 1 h sin heartbeat
        # la PRIMERA consulta purga (nadie está heartbeateando = huérfano)
        self.assertFalse(zt._nuclear_active())
        self.assertFalse(os.path.exists(self._flag()))

    # ---------- fase 4: watchdog ----------

    def _watchdog_env(self, rep=None):
        zt = self.zt
        zt._store("bot-nuclear-report.json").write(rep or {
            "old_pid": 111111, "t0": time.time() - 40, "dur": 40,
            "frenadas": 2, "totales": 2, "mac": 1, "cola": 3,
            "app_ok": True, "notes": []})

        def load_config_fake():
            return {}
        zt.load_config = load_config_fake
        return zt

    def test_watchdog_reporte_final_con_poller_nuevo(self):
        zt = self._watchdog_env()
        zt.subprocess = mock.Mock()
        zt.subprocess.run.return_value = mock.Mock(stdout="%d\n" % os.getpid())
        # "poller nuevo" = este proceso (pid distinto de old_pid 111111)
        zt.cmd_nuclear_watchdog()
        final = self.sends[0]
        self.assertIn("completo y verificado", final)
        self.assertIn("2/2", final)
        self.assertIn("Cola conservada: 3", final)
        self.assertFalse(os.path.exists(self._flag()))  # limpieza SIEMPRE
        self.assertFalse(os.path.exists(zt._nuclear_report_path()))

    def test_watchdog_avisa_si_el_poller_no_volvio(self):
        zt = self._watchdog_env()
        zt.subprocess = mock.Mock()
        zt.subprocess.run.return_value = mock.Mock(stdout="")  # nada vivo
        # reloj inyectado: 1a llamada fija el deadline, las siguientes ya
        # están vencidas → el while ni entra (sin loops infinitos)
        t0 = time.time()
        reloj = mock.Mock()
        reloj.time.side_effect = [t0] + [t0 + 200] * 50
        reloj.sleep = lambda s: None
        zt.time = reloj
        zt.cmd_nuclear_watchdog()
        final = self.sends[0]
        self.assertIn("NO volvió", final)
        self.assertFalse(os.path.exists(self._flag()))  # limpieza SIEMPRE
        self.assertFalse(os.path.exists(zt._nuclear_report_path()))


class TestNuclearEnMenuYHelp(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_menu_incluye_nuclear(self):
        cap = {}

        def fake_tg_api(cfg, method, params=None, timeout=35):
            cap[method] = params

        self.zt.tg_api = fake_tg_api
        cfg = self.zt.load_config()
        cfg["chat_id"] = 4242
        self.assertTrue(self.zt.register_bot_menu(cfg))
        names = [c["command"] for c in cap["setMyCommands"]["commands"]]
        self.assertIn("nuclear", names)

    def test_help_menciona_nuclear(self):
        self.assertIn("/nuclear", self.zt._cmd_help({}, ""))


if __name__ == "__main__":
    unittest.main()
