"""Tests F3: CommandRegistry (semántica de matcheo congelada), rest_of,
build_list_report (Template Method), menú con scope oficial (D1) y el shim.
"""
import os
import sys
import unittest

import support

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zcode_bot.commands import CommandRegistry, rest_of   # noqa: E402
from zcode_bot.reports import build_list_report           # noqa: E402


class TestRestOf(unittest.TestCase):
    def test_parser_unico(self):
        self.assertEqual(rest_of("/task 2"), "2")
        self.assertEqual(rest_of("/task   2  x "), "2  x")
        self.assertEqual(rest_of("/task"), "")
        self.assertEqual(rest_of("  /compact 3 foco "), "3 foco")


class TestRegistrySemantica(unittest.TestCase):
    """Congela la semántica del if-chain original."""

    def setUp(self):
        self.r = CommandRegistry()
        self.calls = []

        @self.r.register("stop", "all_stop")
        def stop(cfg, rest):
            self.calls.append(rest)
            return "OK"

    def test_barra_con_args(self):
        self.assertEqual(self.r.dispatch({}, "/stop"), "OK")
        self.assertEqual(self.r.dispatch({}, "/Stop @ses_x"), "OK")
        self.assertEqual(self.calls[-1], "@ses_x")

    def test_barra_alias(self):
        self.assertEqual(self.r.dispatch({}, "/all_stop"), "OK")

    def test_bare_exacto(self):
        self.assertEqual(self.r.dispatch({}, "stop"), "OK")

    def test_bare_con_args_NO_es_comando(self):
        # "stop ahora" como texto libre NO dispara (así era el original)
        self.assertIsNone(self.r.dispatch({}, "stop ahora"))
        self.assertIsNone(self.r.dispatch({}, "Stop ahora"))

    def test_desconocido_none(self):
        self.assertIsNone(self.r.dispatch({}, "/noexiste"))
        self.assertIsNone(self.r.dispatch({}, ""))


class TestBuildListReport(unittest.TestCase):
    def test_template_completo(self):
        r = build_list_report(
            ["a", "b"],
            "HEADER",
            "EMPTY",
            lambda it, i: "%d. %s" % (i, it),
            lambda it, i: {"text": it, "callback_data": "x:%s" % it},
            "FOOTER")
        txt, kb = r
        self.assertEqual(txt, "HEADER\n1. a\n2. b\n\nFOOTER")
        self.assertEqual([f[0]["callback_data"] for f in kb["inline_keyboard"]],
                         ["x:a", "x:b"])

    def test_vacio(self):
        self.assertEqual(build_list_report([], "H", "EMPTY",
                                           lambda it, i: "", None), "EMPTY")


class TestMenuScopeD1(unittest.TestCase):
    def test_setmycommands_con_scope_del_dueno(self):
        zt = support.fresh(self)
        capturado = {}

        def fake_tg_api(cfg, method, params=None, timeout=35):
            capturado[method] = params

        zt.tg_api = fake_tg_api
        cfg = zt.load_config()
        cfg["chat_id"] = 4242
        self.assertTrue(zt.register_bot_menu(cfg))
        p = capturado["setMyCommands"]
        self.assertEqual(p["scope"], {"type": "chat", "chat_id": 4242})
        self.assertTrue(all(c["command"] for c in p["commands"]))
        self.assertLessEqual(len(p["commands"]), 100)  # límite oficial


class TestShimFino(unittest.TestCase):
    def test_shim_es_fino_y_delega(self):
        with open(support.SHIM) as f:
            contenido = f.read()
        self.assertLess(len(contenido.splitlines()), 25)
        self.assertIn("zcode_bot.gateway", contenido)
        # el entrypoint de subprocesos resuelve al shim desde el paquete
        zt = support.fresh(self)
        self.assertTrue(zt.__file__.endswith("gateway.py"))
        shim_resuelto = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(zt.__file__))),
            "zcode-telegram.py")
        self.assertTrue(os.path.exists(shim_resuelto))


if __name__ == "__main__":
    unittest.main()
