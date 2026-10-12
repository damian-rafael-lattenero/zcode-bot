"""F6: /nuclear — reset de emergencia.

Congela el contrato: confirmación en dos pasos (solo 'go' ejecuta — y 'go'
solo lo produce el botón), callbacks nuc:*, ciclo de reinicio de la app con
procesos y reloj inyectados, y presencia en menú/help.
"""
import os
import sys
import time
import unittest

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
            return [999] if state["host"] else []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertTrue(any("host huérfano" in n for n in rep["notes"]))
        self.assertIn(["pkill", "-TERM", "-f", "zcode-host-local"], calls)

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
    def setUp(self):
        self.zt = support.fresh(self)

    def test_termina_solo_los_zcode_cli_hijos(self):
        calls = []
        hijos = "18181 zcode-cli\n20250 zcode-cli\n777 python\n"

        def run(args, **kw):
            calls.append(args)
            return FakeProc(stdout=hijos) if args[0] == "pgrep" else FakeProc()

        self.zt._nuc_kill_own_runtimes(run=run)
        kills = [c for c in calls if c[0] == "kill"]
        self.assertEqual(kills, [["kill", "-TERM", "18181"],
                                 ["kill", "-TERM", "20250"]])


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
