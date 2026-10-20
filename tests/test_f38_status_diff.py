"""F38 — /status ve los comandos en vuelo + /diff desde el celu.

Damian: "¿podría ser que status me diga qué se está ejecutando ahora
mismo? y agregar el command diff". Desde F37b los comandos lentos corren
en thread y eran INVISIBLES para /status; /diff muestra el último commit
(o los cambios sin commit) del workspace de la task activa.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import reporting as zc_reporting  # noqa: E402
from zcode_bot import limites as lim  # noqa: E402
from zcode_bot.commands import dispatch as zd  # noqa: E402
from zcode_bot.commands import handlers_info as hi  # noqa: E402

CFG = {"token": "T", "chat_id": 1}


def _esperar(cond, timeout=3.0):
    """Espera reactiva corta para el thread del comando (sin busy-wait
    largo: 20 ms de paso, techo chico)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class TestInflight(unittest.TestCase):
    """El registro de comandos lentos en vuelo (dispatch.INFLIGHT)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends = []
        support.parchea(self, zd.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), True)[1])
        support.parchea(self, zd, "SLOW_ASYNC", True)

    def test_aparece_mientras_corre_y_desaparece_al_terminar(self):
        puerta = threading.Event()

        def lento(cfg, text):
            puerta.wait(5)
            return "resultado quota"
        support.parchea(self, zd.REG, "dispatch", lento)
        ack = zd.handle_command(CFG, "/quota")
        self.assertEqual(ack, zd.ACK_SLOW)  # el poller no espera
        self.assertTrue(_esperar(lambda: any(
            b == "quota" for b, _t0, _tx in zd.inflight_snapshot())))
        base, t0, texto = [x for x in zd.inflight_snapshot()
                           if x[0] == "quota"][0]
        self.assertEqual(texto, "/quota")
        self.assertLess(abs(time.time() - t0), 5)
        puerta.set()
        self.assertTrue(_esperar(lambda: not zd.inflight_snapshot()))
        self.assertEqual(self.sends, ["resultado quota"])

    def test_error_tambien_limpia(self):
        def roto(cfg, text):
            raise RuntimeError("boom")
        support.parchea(self, zd.REG, "dispatch", roto)
        zd.handle_command(CFG, "/ctx")
        self.assertTrue(_esperar(lambda: not zd.inflight_snapshot()))
        self.assertTrue(any("Error en /ctx" in s for s in self.sends))


class TestStatusEnVuelo(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_lista_los_comandos_en_vuelo(self):
        rep = zc_reporting.status_report(
            inflight=[("compact", time.time() - 200, "/compact @s"),
                      ("quota", time.time(), "/quota")])
        self.assertIn("⚡", rep)
        self.assertIn("/compact · 3 min", rep)
        self.assertIn("/quota · recién", rep)
        self.assertNotIn("Nada corriendo", rep)

    def test_sin_nada_sigue_diciendo_nada_corriendo(self):
        # fresh() trae un turno de la Mac simulado: lo apago para el caso
        support.parchea(self, zc_reporting, "running_turns", lambda: {})
        rep = zc_reporting.status_report(inflight=[])
        self.assertIn("😌 Nada corriendo.", rep)
        self.assertNotIn("⚡", rep)

    def test_cmd_status_inyecta_el_snapshot_real(self):
        support.parchea(self, zd, "INFLIGHT",
                        {"usage": (time.time(), "/usage")})
        rep = hi._cmd_status(CFG, "")
        self.assertIn("/usage · recién", rep)


class TestDiff(unittest.TestCase):
    def _repo(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def g(*a):
            subprocess.run(["git", "-C", d] + list(a), check=True, env=env,
                           capture_output=True)
        g("init", "-q")
        with open(os.path.join(d, "a.py"), "w") as f:
            f.write("x = 1\n")
        g("add", "a.py")
        g("commit", "-q", "-m", "primer commit de prueba")
        return d, g

    def _activa(self, ws):
        support.parchea(self, hi, "active_bot_session",
                        lambda: {"sid": "s", "ws": ws, "title": "t"})

    def test_ultimo_commit(self):
        d, g = self._repo()
        self._activa(d)
        out = hi._cmd_diff(CFG, "")
        self.assertIn("último commit", out)
        self.assertIn("«primer commit de prueba»", out)
        self.assertIn("1 file changed", out)
        self.assertIn("<pre>", out)
        self.assertIn("+x = 1", out)
        self.assertNotIn("sin commit", out)  # árbol limpio: sin aviso ✏️

    def test_dirty_muestra_los_cambios_sin_commit(self):
        d, g = self._repo()
        self._activa(d)
        with open(os.path.join(d, "a.py"), "a") as f:
            f.write("y = 2\n")
        with open(os.path.join(d, "nuevo.txt"), "w") as f:
            f.write("n\n")
        base = hi._cmd_diff(CFG, "")
        self.assertIn("✏️ 2 archivos con cambios sin commit", base)
        dirty = hi._cmd_diff(CFG, "dirty")
        self.assertIn("cambios SIN commit", dirty)
        self.assertIn("+y = 2", dirty)
        self.assertIn("➕ 1 archivo nuevo sin trackear", dirty)

    def test_asunto_largo_y_patch_grande_entran_en_el_mensaje(self):
        d, g = self._repo()
        self._activa(d)
        with open(os.path.join(d, "b.py"), "w") as f:
            f.write("".join("linea_%04d = %d\n" % (i, i) for i in range(400)))
        g("add", "b.py")
        g("commit", "-q", "-m", "asunto " * 120)  # párrafo como los de la casa
        out = hi._cmd_diff(CFG, "")
        self.assertLessEqual(len(out), lim.CH_CAJA)
        self.assertIn("…", out)                 # asunto capado
        self.assertIn("recortado:", out)       # patch capado con conteo
        self.assertTrue(out.rstrip().endswith("git show)")
                        or "</pre>" in out)

    def test_sin_repo_ni_task(self):
        support.parchea(self, hi, "active_bot_session", lambda: {})
        self.assertIn("Sin task activa", hi._cmd_diff(CFG, ""))
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        self._activa(d)
        self.assertIn("❌ git en", hi._cmd_diff(CFG, ""))


if __name__ == "__main__":
    unittest.main()
