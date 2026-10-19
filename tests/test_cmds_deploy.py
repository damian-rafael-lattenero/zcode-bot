# -*- coding: utf-8 -*-
"""/deploy — parseo de argumentos y confirmación en dos pasos (F25/F28).

'go' corre deploy.sh en un thread (subprocess SIEMPRE de mentira: jamás
el real), 'no' cancela, cualquier otra cosa — o nada — vuelve al menú de
confirmación con los botones dep:go / dep:no. El path de deploy.sh se
resuelve contra el repo; en CI no existe y el fallback tampoco, por eso
las aserciones de path son por sufijo y el run es mock.
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
        fin = time.time() + 5
        while time.time() < fin and not self.sends:
            time.sleep(0.02)
        self.assertTrue(self.sends, "el thread del deploy nunca avisó")

    # -------------------------------------------- confirmación (default)

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
        # solo 'go' y 'no' corren: 'dale', 'yes', 'go go' vuelven al menú
        for rest in ("dale", "yes", "go go", "nada"):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIsInstance(r, tuple)
        self.sub.run.assert_not_called()

    # ------------------------------------------------- 'go' (correr)

    def test_go_corre_deploy_sh_y_reporta_ok(self):
        self.sub.run.return_value = mock.Mock(
            returncode=0, stdout="✓ tests OK\n✓ copiado\n✓ recargado",
            stderr="")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("corriendo", r)
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
        # rest_of ya hace strip; el handler además baja a minúsculas
        self.sub.run.return_value = mock.Mock(returncode=0, stdout="ok",
                                              stderr="")
        for rest in ("GO", "Go ", " go"):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIn("corriendo", r)
        fin = time.time() + 5
        while time.time() < fin and self.sub.run.call_count < 3:
            time.sleep(0.02)
        self.assertEqual(self.sub.run.call_count, 3)

    def test_fallo_de_deploy_reporta_rc_y_tail(self):
        self.sub.run.return_value = mock.Mock(
            returncode=2, stdout="✓ tests OK\n✗ anti-dup falló", stderr="")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("corriendo", r)
        self._espera_send()
        self.assertTrue(any("Deploy falló" in s and "rc=2" in s
                            and "anti-dup" in s for s in self.sends))

    def test_excepcion_del_subprocess_reporta_sin_crash(self):
        # el thread NUNCA muere en silencio: el error va al chat
        self.sub.run.side_effect = RuntimeError("sin bash")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("corriendo", r)
        self._espera_send()
        self.assertTrue(any("deploy" in s and "sin bash" in s
                            for s in self.sends))

    # ------------------------------------------------ 'no' (cancelar)

    def test_no_cancela_sin_correr_nada(self):
        for rest in ("no", "NO", "No "):
            self.assertIn("Cancelado", zc_sist._cmd_deploy({}, rest))
        self.sub.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
