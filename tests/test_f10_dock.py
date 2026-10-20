"""F10 — tiles fantasma del Dock: el runtime standalone se registra en
LaunchServices como app Foreground → tiles por cada turno del celular.
Fix: sandbox nodock.sb que deniega el mach-lookup a launchservicesd (el
proceso queda SIN ASN → invisible; el protocolo stdio no necesita LS y el
sandbox se hereda a los hijos MCP). Validado en vivo 2026-10-13:
session/list, session/resume con 4 MCP y session/events OK bajo sandbox.
Acá se testea la LÓGICA del argv (spawn real con sandbox = humo manual).
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
        """Parchea DOCK_SB a un tmp (existente o no) y restaura."""
        d = tempfile.mkdtemp(prefix="zcsb-")
        path = os.path.join(d, "nodock.sb")
        if existe:
            with open(path, "w") as f:
                f.write("(version 1)(allow default)\n")
        support.parchea(self, zp, "DOCK_SB", path)  # R11: cleanup
        return path

    async def test_argv_propio_no_se_toca(self):
        """Tests/probes que pasan argv explícito jamás se envuelven."""
        self._con_perfil(True)
        propio = ["/bin/echo", "hola"]
        self.assertEqual(zp._spawn_argv(propio), propio)

    async def test_default_con_perfil_se_envuelve(self):
        path = self._con_perfil(True)
        argv = zp._spawn_argv(None)
        self.assertEqual(argv[:3], ["/usr/bin/sandbox-exec", "-f", path])
        self.assertEqual(argv[3:], zp.DEFAULT_ARGV)

    async def test_default_sin_perfil_es_directo(self):
        """Kill switch: sin nodock.sb el spawn es el de siempre."""
        self._con_perfil(False)
        self.assertEqual(zp._spawn_argv(None), zp.DEFAULT_ARGV)

    async def test_default_argv_apunta_al_app_server(self):
        self.assertEqual(zp.DEFAULT_ARGV,
                         [zp.ELECTRON, zp.ZC, "app-server", "--stdio"])

    async def test_perfil_bloquea_launchctl(self):
        """F14: el suicidio por `launchctl kickstart` (incidente 16:13) se
        bloquea a nivel sandbox — process-exec del binario. Regresión: si
        alguien toca nodock.sb y lo pierde, este test explota."""
        with open(os.path.join(TESTS_DIR, "..", "src", "nodock.sb")) as f:
            perfil = f.read()
        self.assertIn('deny process-exec (literal "/bin/launchctl")', perfil)
        self.assertIn('deny mach-lookup (global-name '
                      '"com.apple.coreservices.launchservicesd")', perfil)


if __name__ == "__main__":
    unittest.main()
