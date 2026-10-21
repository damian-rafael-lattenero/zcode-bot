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
from zcode_bot import limits  # noqa: E402
from zcode_bot.commands import dispatch as zd  # noqa: E402
from zcode_bot.commands import handlers_info as hi  # noqa: E402
from zcode_bot.commands import handlers_diff as hd  # R8

CFG = {"token": "T", "chat_id": 1}


def _wait_for(cond, timeout=3.0):
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

    def test_appears_while_running_and_disappears_when_done(self):
        gate = threading.Event()

        def slow_fn(cfg, text):
            gate.wait(5)
            return "resultado quota"
        support.patch(self, zd.REG, "dispatch", slow_fn)
        ack = zd.handle_command(CFG, "/quota")
        self.assertEqual(ack, zd.ACK_SLOW)  # the poller doesn't wait
        self.assertTrue(_wait_for(lambda: any(
            b == "quota" for b, _t0, _tx in zd.inflight_snapshot())))
        base, t0, text = [x for x in zd.inflight_snapshot()
                           if x[0] == "quota"][0]
        self.assertEqual(text, "/quota")
        self.assertLess(abs(time.time() - t0), 5)
        gate.set()
        self.assertTrue(_wait_for(lambda: not zd.inflight_snapshot()))
        self.assertEqual(self.sends, ["resultado quota"])

    def test_error_also_cleans_up(self):
        def broken(cfg, text):
            raise RuntimeError("boom")
        support.patch(self, zd.REG, "dispatch", broken)
        zd.handle_command(CFG, "/ctx")
        self.assertTrue(_wait_for(lambda: not zd.inflight_snapshot()))
        self.assertTrue(any("Error in /ctx" in s for s in self.sends))


class TestStatusInFlight(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_lists_in_flight_commands(self):
        rep = zc_reporting.status_report(
            inflight=[("compact", time.time() - 200, "/compact @s"),
                      ("quota", time.time(), "/quota")])
        self.assertIn("⚡", rep)
        self.assertIn("/compact · 3 min", rep)
        self.assertIn("/quota · just now", rep)
        self.assertNotIn("Nothing running", rep)

    def test_with_nothing_still_says_nothing_running(self):
        # fresh() brings a simulated Mac turn: I turn it off for this case
        support.patch(self, zc_reporting, "running_turns", lambda: {})
        rep = zc_reporting.status_report(inflight=[])
        self.assertIn("😌 Nothing running.", rep)
        self.assertNotIn("⚡", rep)

    def test_cmd_status_injects_real_snapshot(self):
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
        g("commit", "-q", "-m", "first test commit")
        return d, g

    def _activate(self, ws):
        support.patch(self, hd, "active_bot_session",
                        lambda: {"sid": "s", "ws": ws, "title": "t"})

    def test_summary_of_last_commit_with_buttons(self):
        d, g = self._repo()
        self._activate(d)
        h = g("rev-parse", "--short", "HEAD")
        text, markup = hd._cmd_diff(CFG, "")
        # F38e: default = work in progress; clean tree → last commit, with a note
        self.assertIn("🧼 clean tree", text)
        self.assertIn("📦 commit", text)
        self.assertIn("«first test commit»", text)
        self.assertIn("1 file changed", text)
        self.assertIn("1. a.py 🟢 +1 · 1 lines", text)
        self.assertNotIn("<pre>", text)       # the patch lives behind the tap
        kb = markup["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "df:1:%s" % h)
        self.assertEqual(kb[0][0]["text"], "1. a.py")
        # view row: from a commit one jumps to 'in progress' and 'history'
        self.assertEqual([b["callback_data"] for b in kb[-1]], ["dw", "dl"])

    def test_tap_shows_that_files_patch(self):
        d, g = self._repo()
        self._activate(d)
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

    def test_nonexistent_commit_and_out_of_list(self):
        d, g = self._repo()
        self._activate(d)
        self.assertIn("❌ git in", hd._cmd_diff(CFG, "1 deadbee"))  # doesn't exist
        self.assertIn("out of the list (1 to 1)", hd._cmd_diff(CFG, "9"))

    def test_dirty_with_buttons_and_tap(self):
        d, g = self._repo()
        self._activate(d)
        with open(os.path.join(d, "a.py"), "a") as f:
            f.write("y = 2\n")
        with open(os.path.join(d, "nuevo.txt"), "w") as f:
            f.write("n\n")
        text, markup = hd._cmd_diff(CFG, "")  # F38e: default = work in progress
        self.assertIn("✏️ work in progress (no commit)", text)
        self.assertIn("➕ 1 new untracked file", text)
        self.assertIn("1. a.py 🟢 +1 · 2 lines", text)  # +1 in the diff, 2 in total
        self.assertIn("2. ➕ nuevo.txt 🟢 +1 · 1 lines", text)  # new ones at the end
        kb = markup["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "dx:1")
        self.assertEqual(kb[1][0]["callback_data"], "dx:2")
        self.assertEqual([b["callback_data"] for b in kb[-1]], ["dc:HEAD", "dl"])
        self.assertEqual(hd._cmd_diff(CFG, "dirty")[0], text)  # explicit alias
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

    def test_paragraph_subject_and_big_patch_fit(self):
        d, g = self._repo()
        self._activate(d)
        with open(os.path.join(d, "b.py"), "w") as f:
            f.write("".join("linea_%04d = %d\n" % (i, i) for i in range(400)))
        g("add", "b.py")
        g("commit", "-q", "-m", "asunto " * 120)  # a paragraph-long subject like the usual ones
        h = g("rev-parse", "--short", "HEAD")
        text, _ = hd._cmd_diff(CFG, "")
        self.assertLessEqual(len(text), limits.CH_BOX)
        self.assertIn("…", text)  # subject capped
        # F38c: the big file is NOT trimmed — it is split with buttons
        v, kb = hd._cmd_diff(CFG, "1 %s" % h)
        self.assertLessEqual(len(v), limits.CH_BOX)
        self.assertIn("part 1/", v)
        buttons = [b for row in kb["inline_keyboard"] for b in row]
        self.assertEqual(buttons[0]["text"], "[1/%d]" % len(buttons))
        self.assertEqual(buttons[1]["callback_data"], "df:1:%s:2" % h)
        # part 2 arrives via its button and the parts rebuild EVERYTHING
        v2, _ = hd._cmd_diff(CFG, "1 %s 2" % h)
        self.assertIn("part 2/", v2)
        patch = hd._git(d, "show", "--format=", "HEAD", "--", "b.py")
        text = hd._git_diff_colored(patch)
        parts = hd._diff_parts(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(text.startswith("● 🟢 +400  🔴 −0\n⋯ line 1\n🟢 +"))
        self.assertEqual(len(parts), len(buttons))
        self.assertTrue(all(len(x) <= hd._DIFF_PATCH_CAP for x in parts))

    def test_many_files_buttons_capped(self):
        d, g = self._repo()
        self._activate(d)
        for i in range(hd._DIFF_BOTONES_MAX + 2):
            with open(os.path.join(d, "f%02d.txt" % i), "w") as f:
                f.write("%d\n" % i)
        g("add", ".")
        g("commit", "-q", "-m", "muchos")
        text, markup = hd._cmd_diff(CFG, "")
        per_file = [f for f in markup["inline_keyboard"]
                      if f[0]["callback_data"].startswith("df:")]
        self.assertEqual(len(per_file), hd._DIFF_BOTONES_MAX)
        self.assertIn("buttons for the first %d" % hd._DIFF_BOTONES_MAX,
                      text)
        self.assertIn("%d. f21.txt" % (hd._DIFF_BOTONES_MAX + 2), text)

    def test_log_and_specific_commit(self):
        d, g = self._repo()
        self._activate(d)
        old_h = g("rev-parse", "--short", "HEAD")
        with open(os.path.join(d, "b.py"), "w") as f:
            f.write("z = 3\n")
        g("add", "b.py")
        g("commit", "-q", "-m", "second commit")
        text, markup = hd._cmd_diff(CFG, "log")
        self.assertIn("🕘 <b>history</b>", text)
        self.assertIn("1. <code>%s</code>" % g("rev-parse", "--short", "HEAD"),
                      text)
        self.assertIn("2. <code>%s</code> «first test commit»" % old_h,
                      text)
        kb = markup["inline_keyboard"]
        self.assertEqual(kb[1][0]["callback_data"], "dc:%s" % old_h)
        self.assertEqual(kb[-1][0]["callback_data"], "dw")
        # the old commit, pinned: its files, not HEAD's
        tv, mk = hd._cmd_diff(CFG, "commit %s" % old_h)
        self.assertIn("«first test commit»", tv)
        self.assertIn("1. a.py 🟢 +1 · 1 lines", tv)
        self.assertNotIn("b.py", tv)
        self.assertEqual(mk["inline_keyboard"][0][0]["callback_data"],
                         "df:1:%s" % old_h)
        # an old button (df:1:hash) opens ITS commit, doesn't say "changed"
        v = hd._cmd_diff(CFG, "1 %s" % old_h)
        self.assertIn("🟢 +x = 1", v)
        self.assertIn("<code>%s</code>" % old_h, v)

    def test_no_repo_nor_task(self):
        support.patch(self, hd, "active_bot_session", lambda: {})
        self.assertIn("No active task", hd._cmd_diff(CFG, ""))
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        self._activate(d)
        self.assertIn("❌ git in", hd._cmd_diff(CFG, ""))


class TestColorizeDiff(unittest.TestCase):
    """F38d — ONE renderer for the live box and /diff."""

    def test_git_to_colored_dots_with_hunk_separators(self):
        patch = ("diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n"
                 "@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n"
                 "@@ -40,1 +40,2 @@\n ctx2\n+more\n")
        text = hd._git_diff_colored(patch)
        self.assertEqual(text.split("\n")[:-1], [
            "● 🟢 +2  🔴 −1", "⋯ line 1", "·  ctx", "🔴 -old", "🟢 +new",
            "⋯ line 40", "·  ctx2", "🟢 +more"])
        self.assertIsNone(hd._git_diff_colored("Binary files a and b differ"))

    def test_live_box_still_the_same_without_hunks(self):
        from zcode_bot.messages import _colored_diff
        out = _colored_diff(["a", "b"], ["a", "c"])
        self.assertEqual(out, "● 🟢 +1  🔴 −1\n·  a\n🔴 -b\n🟢 +c")
        self.assertNotIn("⋯", out)


class TestDiffButtons(unittest.TestCase):
    def test_cb_text(self):
        from zcode_bot.updates import _cb_text
        self.assertEqual(_cb_text("df:2:abc1234"), "/diff 2 abc1234")
        self.assertEqual(_cb_text("dx:3"), "/diff dirty 3")
        self.assertEqual(_cb_text("df:2:abc1234:4"), "/diff 2 abc1234 4")
        self.assertEqual(_cb_text("dx:3:2"), "/diff dirty 3 2")
        self.assertEqual(_cb_text("dc:abc1234"), "/diff commit abc1234")
        self.assertEqual(_cb_text("dl"), "/diff log")
        self.assertEqual(_cb_text("dw"), "/diff")


class TestDiffParts(unittest.TestCase):
    """F38c — EQUAL parts split by lines, under CAP, without losing a byte."""

    def test_small_single_part(self):
        self.assertEqual(hd._diff_parts("a\nb\n"), ["a\nb\n"])

    def test_equal_complete_and_under_cap(self):
        patch = "".join("+line %05d with some text\n" % i
                        for i in range(400))
        parts = hd._diff_parts(patch)
        self.assertEqual("".join(parts), patch)
        self.assertTrue(all(len(x) <= hd._DIFF_PATCH_CAP for x in parts))
        self.assertEqual(len(parts), -(-len(patch) // hd._DIFF_PATCH_CAP))
        # equal: no part is a runt (the tail gets absorbed)
        self.assertGreater(min(map(len, parts)), hd._DIFF_PATCH_CAP // 2)
        # by lines: every part ends with a newline
        self.assertTrue(all(x.endswith("\n") for x in parts))

    def test_monster_line_hard_cut(self):
        m = "x" * 9000 + "\n"
        parts = hd._diff_parts(m)
        self.assertEqual("".join(parts), m)
        self.assertTrue(all(len(x) <= hd._DIFF_PATCH_CAP for x in parts))

    def test_part_buttons_rows_of_5_and_current_marked(self):
        kb = hd._part_buttons(3, "abc1234", False, 7, 6)["inline_keyboard"]
        self.assertEqual([len(f) for f in kb], [5, 2])
        flat = [b for f in kb for b in f]
        self.assertEqual(flat[5]["text"], "[6/7]")
        self.assertEqual(flat[0]["callback_data"], "df:3:abc1234:1")
        kbd = hd._part_buttons(2, "abc1234", True, 2, 1)["inline_keyboard"]
        self.assertEqual(kbd[0][1]["callback_data"], "dx:2:2")


if __name__ == "__main__":
    unittest.main()
