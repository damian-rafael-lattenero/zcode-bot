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
        self.assertIn("Ejecutando", r)
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
