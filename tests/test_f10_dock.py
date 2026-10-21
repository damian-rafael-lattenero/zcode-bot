"""F10 — ghost Dock tiles: the standalone runtime registers with
LaunchServices as a Foreground app → one tile per phone turn. Fix:
nodock.sb sandbox that denies the mach-lookup to launchservicesd (the
process stays WITHOUT an ASN → invisible; the stdio protocol doesn't
need LS and the sandbox is inherited by the MCP children). Validated
live 2026-10-13: session/list, session/resume with 4 MCPs and
session/events OK under sandbox. This file tests the argv LOGIC (a real
sandboxed spawn = manual smoke test).
"""
import os
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
import zcode_proto as zp  # noqa: E402


class TestSpawnArgv(unittest.IsolatedAsyncioTestCase):
    def _con_perfil(self, existe):
        """Patches DOCK_SB to a tmp path (existing or not) and restores."""
        d = tempfile.mkdtemp(prefix="zcsb-")
        path = os.path.join(d, "nodock.sb")
        if existe:
            with open(path, "w") as f:
                f.write("(version 1)(allow default)\n")
        support.parchea(self, zp, "DOCK_SB", path)  # R11: cleanup
        return path

    async def test_argv_propio_no_se_toca(self):
        """Tests/probes passing an explicit argv are never wrapped."""
        self._con_perfil(True)
        propio = ["/bin/echo", "hola"]
        self.assertEqual(zp._spawn_argv(propio), propio)

    async def test_default_con_perfil_se_envuelve(self):
        path = self._con_perfil(True)
        argv = zp._spawn_argv(None)
        self.assertEqual(argv[:3], ["/usr/bin/sandbox-exec", "-f", path])
        self.assertEqual(argv[3:], zp.DEFAULT_ARGV)

    async def test_default_sin_perfil_es_directo(self):
        """Kill switch: without nodock.sb the spawn is the usual one."""
        self._con_perfil(False)
        self.assertEqual(zp._spawn_argv(None), zp.DEFAULT_ARGV)

    async def test_default_argv_apunta_al_app_server(self):
        self.assertEqual(zp.DEFAULT_ARGV,
                         [zp.ELECTRON, zp.ZC, "app-server", "--stdio"])

    async def test_perfil_bloquea_launchctl(self):
        """F14: the `launchctl kickstart` suicide (16:13 incident) is
        blocked at the sandbox level — process-exec of the binary.
        Regression: if someone edits nodock.sb and loses it, this test
        blows up."""
        with open(os.path.join(TESTS_DIR, "..", "src", "nodock.sb")) as f:
            perfil = f.read()
        self.assertIn('deny process-exec (literal "/bin/launchctl")', perfil)
        self.assertIn('deny mach-lookup (global-name '
                      '"com.apple.coreservices.launchservicesd")', perfil)


if __name__ == "__main__":
    unittest.main()
