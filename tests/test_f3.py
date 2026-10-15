"""Tests F3: CommandRegistry (semántica de matcheo congelada), rest_of,
build_list_report (Template Method), menú con scope oficial (D1) y el shim.
"""
import os
import sys
import unittest
from unittest import mock

import support

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from zcode_bot import config as zc_config  # noqa: E402

from zcode_bot.commands import CommandRegistry, rest_of   # noqa: E402
from zcode_bot.reports import build_list_report           # noqa: E402
from zcode_bot import models as zc_models                 # noqa: E402


class TestRestOf(unittest.IsolatedAsyncioTestCase):
    async def test_parser_unico(self):
        self.assertEqual(rest_of("/task 2"), "2")
        self.assertEqual(rest_of("/task   2  x "), "2  x")
        self.assertEqual(rest_of("/task"), "")
        self.assertEqual(rest_of("  /compact 3 foco "), "3 foco")


class TestRegistrySemantica(unittest.IsolatedAsyncioTestCase):
    """Congela la semántica del if-chain original."""

    def setUp(self):
        self.r = CommandRegistry()
        self.calls = []

        @self.r.register("stop", "all_stop")
        def stop(cfg, rest):
            self.calls.append(rest)
            return "OK"

    async def test_barra_con_args(self):
        self.assertEqual(self.r.dispatch({}, "/stop"), "OK")
        self.assertEqual(self.r.dispatch({}, "/Stop @ses_x"), "OK")
        self.assertEqual(self.calls[-1], "@ses_x")

    async def test_barra_alias(self):
        self.assertEqual(self.r.dispatch({}, "/all_stop"), "OK")

    async def test_bare_exacto(self):
        self.assertEqual(self.r.dispatch({}, "stop"), "OK")

    async def test_bare_con_args_NO_es_comando(self):
        # "stop ahora" como texto libre NO dispara (así era el original)
        self.assertIsNone(self.r.dispatch({}, "stop ahora"))
        self.assertIsNone(self.r.dispatch({}, "Stop ahora"))

    async def test_desconocido_none(self):
        self.assertIsNone(self.r.dispatch({}, "/noexiste"))
        self.assertIsNone(self.r.dispatch({}, ""))


class TestBuildListReport(unittest.IsolatedAsyncioTestCase):
    async def test_template_completo(self):
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

    async def test_vacio(self):
        self.assertEqual(build_list_report([], "H", "EMPTY",
                                           lambda it, i: "", None), "EMPTY")


class TestMenuScopeD1(unittest.IsolatedAsyncioTestCase):
    async def test_setmycommands_con_scope_del_dueno(self):
        zt = support.fresh(self)
        capturado = {}

        def fake_tg_api(cfg, method, params=None, timeout=35):
            capturado[method] = params

        # F4i: register_bot_menu vive en commands.dispatch y usa uhttp.tg_api
        from zcode_bot.utils import http as zc_http
        zc_http.tg_api = fake_tg_api
        try:
            cfg = zt.load_config()
            cfg["chat_id"] = 4242
            self.assertTrue(zt.register_bot_menu(cfg))
        finally:
            from zcode_bot.utils.http import tg_api as _real
            zc_http.tg_api = _real
        p = capturado["setMyCommands"]
        self.assertEqual(p["scope"], {"type": "chat", "chat_id": 4242})
        self.assertTrue(all(c["command"] for c in p["commands"]))
        self.assertLessEqual(len(p["commands"]), 100)  # límite oficial


class TestShimFino(unittest.IsolatedAsyncioTestCase):
    async def test_shim_es_fino_y_delega(self):
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


class TestF4Hardening(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_thinking_por_modelo(self):
        # GLM-5.3 y Flash comparten set; un modelo desconocido cae al default
        self.assertEqual(zc_models._thinking_levels("GLM-5.3"), ("max", "high", "low"))
        self.assertEqual(zc_models._thinking_levels("GLM-5.3-Flash"), ("max", "high", "low"))
        self.assertEqual(zc_models._thinking_levels("GLM-6.0"), ("max", "high", "low"))
        # un futuro modelo con set propio se respeta (patch.dict: muta y
        # restaura — el catálogo es global para todo el proceso)
        with mock.patch.dict(
                zc_models.MODEL_THINKING,
                {"GLM-5.2": ("max", "xhigh", "high", "medium",
                             "low", "minimal", "none")}):
            self.assertIn("xhigh", zc_models._thinking_levels("GLM-5.2"))

    async def test_busy_derivado_de_wait(self):
        # D2: la ventana sigue la config, no un fijo menor que el wait
        import time as _t
        busy = os.path.join(zc_config.STATE_DIR, "tg-busy")
        with open(busy, "w") as f:
            f.write(str(_t.time() - 200))       # mtime = hace 200 s
        os.utime(busy, (_t.time() - 200, _t.time() - 200))
        self.assertTrue(self.zt.busy_fresh())   # 200 < 240+60 → sigue busy
        os.utime(busy, (_t.time() - 400, _t.time() - 400))
        self.assertFalse(self.zt.busy_fresh())  # 400 > 300 → liberado
        from zcode_bot import updates as zc_updates
        zc_updates.busy_path = lambda: "/no/existe"
        self.assertFalse(self.zt.busy_fresh())  # sin archivo → no busy
