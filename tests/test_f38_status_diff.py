"""F38 — /status sees the in-flight commands + /diff from the phone.

Damian: "could status tell me what is running right this moment? and
add the command diff". Since F37b, slow commands run in a thread and
were INVISIBLE to /status; /diff shows the last commit (or the
uncommitted changes) of the active task's workspace.
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
from zcode_bot.commands import handlers_diff as hd  # R8

CFG = {"token": "T", "chat_id": 1}


def _esperar(cond, timeout=3.0):
    """Short reactive wait — single body in support.wait_until (R12)."""
    return support.wait_until(cond, timeout=timeout)


class TestInflight(unittest.TestCase):
    """The registry of slow in-flight commands (dispatch.INFLIGHT)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends = []
        support.patch(self, zd.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), True)[1])
        support.patch(self, zd, "SLOW_ASYNC", True)

    def test_aparece_mientras_corre_y_desaparece_al_terminar(self):
        puerta = threading.Event()

        def lento(cfg, text):
            puerta.wait(5)
            return "resultado quota"
        support.patch(self, zd.REG, "dispatch", lento)
        ack = zd.handle_command(CFG, "/quota")
        self.assertEqual(ack, zd.ACK_SLOW)  # the poller doesn't wait
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
        support.patch(self, zd.REG, "dispatch", roto)
        zd.handle_command(CFG, "/ctx")
        self.assertTrue(_esperar(lambda: not zd.inflight_snapshot()))
        self.assertTrue(any("Error in /ctx" in s for s in self.sends))


class TestStatusEnVuelo(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_lista_los_comandos_en_vuelo(self):
        rep = zc_reporting.status_report(
            inflight=[("compact", time.time() - 200, "/compact @s"),
                      ("quota", time.time(), "/quota")])
        self.assertIn("⚡", rep)
        self.assertIn("/compact · 3 min", rep)
        self.assertIn("/quota · just now", rep)
        self.assertNotIn("Nothing running", rep)

    def test_sin_nada_sigue_diciendo_nada_corriendo(self):
        # fresh() brings a simulated Mac turn: I turn it off for this case
        support.patch(self, zc_reporting, "running_turns", lambda: {})
        rep = zc_reporting.status_report(inflight=[])
        self.assertIn("😌 Nothing running.", rep)
        self.assertNotIn("⚡", rep)

    def test_cmd_status_inyecta_el_snapshot_real(self):
        support.patch(self, zd, "INFLIGHT",
                        {"usage": (time.time(), "/usage")})
        rep = hi._cmd_status(CFG, "")
        self.assertIn("/usage · just now", rep)


class TestDiff(unittest.TestCase):
    """F38b — two levels: a summary with ONE BUTTON per file (never
    truncated) and the patch view of ONE file on tap."""

    def _repo(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def g(*a):
            return subprocess.run(["git", "-C", d] + list(a), check=True,
                                  env=env, capture_output=True,
                                  text=True).stdout.strip()
        g("init", "-q")
        with open(os.path.join(d, "a.py"), "w") as f:
            f.write("x = 1\n")
        g("add", "a.py")
        g("commit", "-q", "-m", "primer commit de prueba")
        return d, g

    def _activa(self, ws):
        support.patch(self, hd, "active_bot_session",
                        lambda: {"sid": "s", "ws": ws, "title": "t"})

    def test_resumen_del_ultimo_commit_con_botones(self):
        d, g = self._repo()
        self._activa(d)
        h = g("rev-parse", "--short", "HEAD")
        texto, markup = hd._cmd_diff(CFG, "")
        # F38e: default = work in progress; clean tree → last commit, with a note
        self.assertIn("🧼 clean tree", texto)
        self.assertIn("📦 commit", texto)
        self.assertIn("«primer commit de prueba»", texto)
        self.assertIn("1 file changed", texto)
        self.assertIn("1. a.py 🟢 +1 · 1 lines", texto)
        self.assertNotIn("<pre>", texto)       # the patch lives behind the tap
        kb = markup["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "df:1:%s" % h)
        self.assertEqual(kb[0][0]["text"], "1. a.py")
        # view row: from a commit one jumps to 'in progress' and 'history'
        self.assertEqual([b["callback_data"] for b in kb[-1]], ["dw", "dl"])

    def test_tap_muestra_el_patch_de_ese_archivo(self):
        d, g = self._repo()
        self._activa(d)
        h = g("rev-parse", "--short", "HEAD")
        v = hd._cmd_diff(CFG, "1 %s" % h)
        self.assertIn("📄 <b>a.py</b> · 🟢 +1 · 1 lines", v)
        self.assertIn("<pre>", v)
        # F38d: same render as the live box — colored dots, not dry +/-
        self.assertIn("● 🟢 +1  🔴 −0", v)
        self.assertIn("🟢 +x = 1", v)
        self.assertIn("⋯ line 1", v)
        self.assertNotIn("diff --git", v)
        self.assertNotIn("+++ ", v)

    def test_commit_inexistente_y_fuera_de_lista(self):
        d, g = self._repo()
        self._activa(d)
        self.assertIn("❌ git in", hd._cmd_diff(CFG, "1 deadbee"))  # doesn't exist
        self.assertIn("out of the list (1 to 1)", hd._cmd_diff(CFG, "9"))

    def test_dirty_con_botones_y_tap(self):
        d, g = self._repo()
        self._activa(d)
        with open(os.path.join(d, "a.py"), "a") as f:
            f.write("y = 2\n")
        with open(os.path.join(d, "nuevo.txt"), "w") as f:
            f.write("n\n")
        texto, markup = hd._cmd_diff(CFG, "")  # F38e: default = work in progress
        self.assertIn("✏️ work in progress (no commit)", texto)
        self.assertIn("➕ 1 new untracked file", texto)
        self.assertIn("1. a.py 🟢 +1 · 2 lines", texto)  # +1 in the diff, 2 in total
        self.assertIn("2. ➕ nuevo.txt 🟢 +1 · 1 lines", texto)  # new ones at the end
        kb = markup["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "dx:1")
        self.assertEqual(kb[1][0]["callback_data"], "dx:2")
        self.assertEqual([b["callback_data"] for b in kb[-1]], ["dc:HEAD", "dl"])
        self.assertEqual(hd._cmd_diff(CFG, "dirty")[0], texto)  # explicit alias
        v = hd._cmd_diff(CFG, "dirty 1")
        self.assertIn("🟢 +y = 2", v)
        self.assertIn("·  x = 1", v)  # context with the middle dot
        self.assertIn("in progress", v)
        v2 = hd._cmd_diff(CFG, "dirty 2")  # the new file: all additions
        self.assertIn("➕ new", v2)
        self.assertIn("🟢 +n", v2)
        # the commit stays reachable explicitly, without the clean-tree note
        tc, _ = hd._cmd_diff(CFG, "commit")
        self.assertIn("📦 commit", tc)
        self.assertNotIn("clean tree", tc)

    def test_asunto_parrafo_y_patch_grande_entran(self):
        d, g = self._repo()
        self._activa(d)
        with open(os.path.join(d, "b.py"), "w") as f:
            f.write("".join("linea_%04d = %d\n" % (i, i) for i in range(400)))
        g("add", "b.py")
        g("commit", "-q", "-m", "asunto " * 120)  # a paragraph-long subject like the usual ones
        h = g("rev-parse", "--short", "HEAD")
        texto, _ = hd._cmd_diff(CFG, "")
        self.assertLessEqual(len(texto), lim.CH_CAJA)
        self.assertIn("…", texto)  # subject capped
        # F38c: the big file is NOT trimmed — it is split with buttons
        v, kb = hd._cmd_diff(CFG, "1 %s" % h)
        self.assertLessEqual(len(v), lim.CH_CAJA)
        self.assertIn("part 1/", v)
        botones = [b for fila in kb["inline_keyboard"] for b in fila]
        self.assertEqual(botones[0]["text"], "[1/%d]" % len(botones))
        self.assertEqual(botones[1]["callback_data"], "df:1:%s:2" % h)
        # part 2 arrives via its button and the parts rebuild EVERYTHING
        v2, _ = hd._cmd_diff(CFG, "1 %s 2" % h)
        self.assertIn("part 2/", v2)
        patch = hd._git(d, "show", "--format=", "HEAD", "--", "b.py")
        texto = hd._diff_coloreado_git(patch)
        partes = hd._diff_partes(texto)
        self.assertEqual("".join(partes), texto)
        self.assertTrue(texto.startswith("● 🟢 +400  🔴 −0\n⋯ line 1\n🟢 +"))
        self.assertEqual(len(partes), len(botones))
        self.assertTrue(all(len(x) <= hd._DIFF_PATCH_CAP for x in partes))

    def test_muchos_archivos_botones_capados(self):
        d, g = self._repo()
        self._activa(d)
        for i in range(hd._DIFF_BOTONES_MAX + 2):
            with open(os.path.join(d, "f%02d.txt" % i), "w") as f:
                f.write("%d\n" % i)
        g("add", ".")
        g("commit", "-q", "-m", "muchos")
        texto, markup = hd._cmd_diff(CFG, "")
        de_archivo = [f for f in markup["inline_keyboard"]
                      if f[0]["callback_data"].startswith("df:")]
        self.assertEqual(len(de_archivo), hd._DIFF_BOTONES_MAX)
        self.assertIn("buttons for the first %d" % hd._DIFF_BOTONES_MAX,
                      texto)
        self.assertIn("%d. f21.txt" % (hd._DIFF_BOTONES_MAX + 2), texto)

    def test_log_y_commit_puntual(self):
        d, g = self._repo()
        self._activa(d)
        viejo_h = g("rev-parse", "--short", "HEAD")
        with open(os.path.join(d, "b.py"), "w") as f:
            f.write("z = 3\n")
        g("add", "b.py")
        g("commit", "-q", "-m", "segundo commit")
        texto, markup = hd._cmd_diff(CFG, "log")
        self.assertIn("🕘 <b>history</b>", texto)
        self.assertIn("1. <code>%s</code>" % g("rev-parse", "--short", "HEAD"),
                      texto)
        self.assertIn("2. <code>%s</code> «primer commit de prueba»" % viejo_h,
                      texto)
        kb = markup["inline_keyboard"]
        self.assertEqual(kb[1][0]["callback_data"], "dc:%s" % viejo_h)
        self.assertEqual(kb[-1][0]["callback_data"], "dw")
        # the old commit, pinned: its files, not HEAD's
        tv, mk = hd._cmd_diff(CFG, "commit %s" % viejo_h)
        self.assertIn("«primer commit de prueba»", tv)
        self.assertIn("1. a.py 🟢 +1 · 1 lines", tv)
        self.assertNotIn("b.py", tv)
        self.assertEqual(mk["inline_keyboard"][0][0]["callback_data"],
                         "df:1:%s" % viejo_h)
        # an old button (df:1:hash) opens ITS commit, doesn't say "cambió"
        v = hd._cmd_diff(CFG, "1 %s" % viejo_h)
        self.assertIn("🟢 +x = 1", v)
        self.assertIn("<code>%s</code>" % viejo_h, v)

    def test_sin_repo_ni_task(self):
        support.patch(self, hd, "active_bot_session", lambda: {})
        self.assertIn("No active task", hd._cmd_diff(CFG, ""))
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        self._activa(d)
        self.assertIn("❌ git in", hd._cmd_diff(CFG, ""))


class TestColorearDiff(unittest.TestCase):
    """F38d — ONE renderer for the live box and /diff."""

    def test_git_a_globitos_con_separadores_de_hunk(self):
        patch = ("diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n"
                 "@@ -1,2 +1,2 @@\n ctx\n-viejo\n+nuevo\n"
                 "@@ -40,1 +40,2 @@\n ctx2\n+mas\n")
        texto = hd._diff_coloreado_git(patch)
        self.assertEqual(texto.split("\n")[:-1], [
            "● 🟢 +2  🔴 −1", "⋯ line 1", "·  ctx", "🔴 -viejo", "🟢 +nuevo",
            "⋯ line 40", "·  ctx2", "🟢 +mas"])
        self.assertIsNone(hd._diff_coloreado_git("Binary files a and b differ"))

    def test_la_caja_viva_sigue_igual_sin_hunks(self):
        from zcode_bot.messages import _diff_coloreado
        out = _diff_coloreado(["a", "b"], ["a", "c"])
        self.assertEqual(out, "● 🟢 +1  🔴 −1\n·  a\n🔴 -b\n🟢 +c")
        self.assertNotIn("⋯", out)


class TestBotonesDiff(unittest.TestCase):
    def test_cb_text(self):
        from zcode_bot.updates import _cb_text
        self.assertEqual(_cb_text("df:2:abc1234"), "/diff 2 abc1234")
        self.assertEqual(_cb_text("dx:3"), "/diff dirty 3")
        self.assertEqual(_cb_text("df:2:abc1234:4"), "/diff 2 abc1234 4")
        self.assertEqual(_cb_text("dx:3:2"), "/diff dirty 3 2")
        self.assertEqual(_cb_text("dc:abc1234"), "/diff commit abc1234")
        self.assertEqual(_cb_text("dl"), "/diff log")
        self.assertEqual(_cb_text("dw"), "/diff")


class TestDiffPartes(unittest.TestCase):
    """F38c — EQUAL parts split by lines, under CAP, without losing a byte."""

    def test_chico_una_sola_parte(self):
        self.assertEqual(hd._diff_partes("a\nb\n"), ["a\nb\n"])

    def test_iguales_completas_y_bajo_cap(self):
        patch = "".join("+linea %05d con algo de texto\n" % i
                        for i in range(400))
        partes = hd._diff_partes(patch)
        self.assertEqual("".join(partes), patch)
        self.assertTrue(all(len(x) <= hd._DIFF_PATCH_CAP for x in partes))
        self.assertEqual(len(partes), -(-len(patch) // hd._DIFF_PATCH_CAP))
        # equal: no part is a runt (the tail gets absorbed)
        self.assertGreater(min(map(len, partes)), hd._DIFF_PATCH_CAP // 2)
        # by lines: every part ends with a newline
        self.assertTrue(all(x.endswith("\n") for x in partes))

    def test_linea_monstruo_se_corta_en_duro(self):
        m = "x" * 9000 + "\n"
        partes = hd._diff_partes(m)
        self.assertEqual("".join(partes), m)
        self.assertTrue(all(len(x) <= hd._DIFF_PATCH_CAP for x in partes))

    def test_botones_de_partes_filas_de_5_y_actual_marcada(self):
        kb = hd._botones_partes(3, "abc1234", False, 7, 6)["inline_keyboard"]
        self.assertEqual([len(f) for f in kb], [5, 2])
        plano = [b for f in kb for b in f]
        self.assertEqual(plano[5]["text"], "[6/7]")
        self.assertEqual(plano[0]["callback_data"], "df:3:abc1234:1")
        kbd = hd._botones_partes(2, "abc1234", True, 2, 1)["inline_keyboard"]
        self.assertEqual(kbd[0][1]["callback_data"], "dx:2:2")


if __name__ == "__main__":
    unittest.main()
