# -*- coding: utf-8 -*-
"""/deploy — argument parsing and two-step confirmation (F25/F28).

'go' runs deploy.sh in a thread (subprocess is ALWAYS fake: never the
real one), 'no' cancels, anything else — or nothing — goes back to the
confirmation menu with the dep:go / dep:no buttons. The deploy.sh path
is resolved against the repo; in CI it does not exist and neither does
the fallback, which is why the path assertions are by suffix and the
run is mocked.
"""
import time
import unittest
from unittest import mock

import support
from zcode_bot.commands.handlers_sistema import _DEPLOY_TIMEOUT_S  # noqa: E402
from zcode_bot.commands import handlers_sistema as zc_sist
from zcode_bot.utils import http as zc_http


class TestCmdDeploy(unittest.TestCase):
    def setUp(self):
        self.sends = []
        support.parchea(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), True)[1])
        self.sub = support.parchea(self, zc_sist, "subprocess",
                                   mock.Mock())

    def _espera_send(self):
        self.assertTrue(support.esperar(lambda: bool(self.sends)),
                        "el thread del deploy nunca avisó")

    # -------------------------------------------- confirmation (default)

    def test_sin_args_pide_confirmacion(self):
        for rest in ("", "   ", None):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIsInstance(r, tuple)
            texto, markup = r
            self.assertIn("deploy.sh", texto)
            self.assertEqual([b["callback_data"]
                              for fila in markup["inline_keyboard"]
                              for b in fila], ["dep:go", "dep:no"])
        self.sub.run.assert_not_called()

    def test_args_raros_piden_confirmacion(self):
        # only 'go' and 'no' run: 'dale', 'yes', 'go go' go back to the menu
        for rest in ("dale", "yes", "go go", "nada"):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIsInstance(r, tuple)
        self.sub.run.assert_not_called()

    # ------------------------------------------------- 'go' (run)

    def test_go_corre_deploy_sh_y_reporta_ok(self):
        self.sub.run.return_value = mock.Mock(
            returncode=0, stdout="✓ tests OK\n✓ copiado\n✓ recargado",
            stderr="")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("running", r)
        self._espera_send()
        (args, kwargs) = self.sub.run.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "bash")
        self.assertTrue(cmd[1].endswith("deploy.sh"), cmd)
        self.assertEqual(kwargs.get("timeout"), _DEPLOY_TIMEOUT_S)
        self.assertTrue(kwargs.get("capture_output"))
        self.assertTrue(kwargs.get("text"))
        self.assertTrue(any("Deploy OK" in s and "recargado" in s
                            for s in self.sends))

    def test_go_es_case_insensitive_y_tolera_espacios(self):
        # rest_of already strips; the handler also lowercases
        self.sub.run.return_value = mock.Mock(returncode=0, stdout="ok",
                                              stderr="")
        for rest in ("GO", "Go ", " go"):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIn("running", r)
        self.assertTrue(
            support.esperar(lambda: self.sub.run.call_count >= 3))
        self.assertEqual(self.sub.run.call_count, 3)

    def test_fallo_de_deploy_reporta_rc_y_tail(self):
        self.sub.run.return_value = mock.Mock(
            returncode=2, stdout="✓ tests OK\n✗ anti-dup falló", stderr="")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("running", r)
        self._espera_send()
        self.assertTrue(any("Deploy failed" in s and "rc=2" in s
                            and "anti-dup" in s for s in self.sends))

    def test_excepcion_del_subprocess_reporta_sin_crash(self):
        # the thread NEVER dies silently: the error goes to the chat
        self.sub.run.side_effect = RuntimeError("sin bash")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("running", r)
        self._espera_send()
        self.assertTrue(any("deploy" in s and "sin bash" in s
                            for s in self.sends))

    # ------------------------------------------------ 'no' (cancel)

    def test_no_cancela_sin_correr_nada(self):
        for rest in ("no", "NO", "No "):
            self.assertIn("Cancelled", zc_sist._cmd_deploy({}, rest))
        self.sub.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
