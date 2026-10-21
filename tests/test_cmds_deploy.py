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
        support.patch(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), True)[1])
        self.sub = support.patch(self, zc_sist, "subprocess",
                                   mock.Mock())

    def _wait_for_send(self):
        self.assertTrue(support.wait_until(lambda: bool(self.sends)),
                        "el thread del deploy nunca avisó")

    # -------------------------------------------- confirmation (default)

    def test_no_args_asks_for_confirmation(self):
        for rest in ("", "   ", None):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIsInstance(r, tuple)
            text, markup = r
            self.assertIn("deploy.sh", text)
            self.assertEqual([b["callback_data"]
                              for row in markup["inline_keyboard"]
                              for b in row], ["dep:go", "dep:no"])
        self.sub.run.assert_not_called()

    def test_odd_args_ask_for_confirmation(self):
        # only 'go' and 'no' run: 'dale', 'yes', 'go go' go back to the menu
        for rest in ("dale", "yes", "go go", "nada"):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIsInstance(r, tuple)
        self.sub.run.assert_not_called()

    # ------------------------------------------------- 'go' (run)

    def test_go_runs_deploy_sh_and_reports_ok(self):
        self.sub.run.return_value = mock.Mock(
            returncode=0, stdout="✓ tests OK\n✓ copiado\n✓ recargado",
            stderr="")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("running", r)
        self._wait_for_send()
        (args, kwargs) = self.sub.run.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "bash")
        self.assertTrue(cmd[1].endswith("deploy.sh"), cmd)
        self.assertEqual(kwargs.get("timeout"), _DEPLOY_TIMEOUT_S)
        self.assertTrue(kwargs.get("capture_output"))
        self.assertTrue(kwargs.get("text"))
        self.assertTrue(any("Deploy OK" in s and "recargado" in s
                            for s in self.sends))

    def test_go_is_case_insensitive_and_tolerates_spaces(self):
        # rest_of already strips; the handler also lowercases
        self.sub.run.return_value = mock.Mock(returncode=0, stdout="ok",
                                              stderr="")
        for rest in ("GO", "Go ", " go"):
            r = zc_sist._cmd_deploy({}, rest)
            self.assertIn("running", r)
        self.assertTrue(
            support.wait_until(lambda: self.sub.run.call_count >= 3))
        self.assertEqual(self.sub.run.call_count, 3)

    def test_deploy_failure_reports_rc_and_tail(self):
        self.sub.run.return_value = mock.Mock(
            returncode=2, stdout="✓ tests OK\n✗ anti-dup falló", stderr="")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("running", r)
        self._wait_for_send()
        self.assertTrue(any("Deploy failed" in s and "rc=2" in s
                            and "anti-dup" in s for s in self.sends))

    def test_subprocess_exception_reports_without_crash(self):
        # the thread NEVER dies silently: the error goes to the chat
        self.sub.run.side_effect = RuntimeError("sin bash")
        r = zc_sist._cmd_deploy({}, "go")
        self.assertIn("running", r)
        self._wait_for_send()
        self.assertTrue(any("deploy" in s and "sin bash" in s
                            for s in self.sends))

    # ------------------------------------------------ 'no' (cancel)

    def test_no_cancels_without_running_anything(self):
        for rest in ("no", "NO", "No "):
            self.assertIn("Cancelled", zc_sist._cmd_deploy({}, rest))
        self.sub.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
