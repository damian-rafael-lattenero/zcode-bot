"""GOLDEN TESTS — freeze the current UX of the bot's commands.

Purpose (F0 of the refactor): any visible behavior change is caught
here BEFORE reaching production. If a golden breaks because of an
intentional improvement, it is updated EXPLICITLY in the
corresponding commit.

Excluded commands (they touch real network/runtime): /quota, /fork,
/api, /run, /task new, and the variants that EXECUTE compact/stop (the
"no args" ones that only list do get in).
"""
import json
import os
import re
import unittest

import support

from zcode_bot import config as zc_config  # noqa: E402


def norm(s):
    """Normalizes dynamic parts (HH:MM times) for comparison."""
    return re.sub(r"\d{2}:\d{2}", "HH:MM", s)


class GoldenCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.env_root = zc_config.BASE

    def cmd(self, text):
        return self.zt.handle_command({}, text)

    # ---------------- lists ----------------

    async def test_task_projects(self):
        r = self.cmd("/task")
        self.assertIsInstance(r, tuple)
        txt, kb = r
        self.assertEqual(
            txt,
            "💬 <b>Conversations by project</b> — tap one (or /task n)\n"
            "· 1. MCP · 📁haskell\n"
            "🔄 2. testing\n"
            "· 3. CV · 📁cv\n"
            "\n"
            "Free text you send goes to the active conversation (🎯).")
        buttons = [f[0] for f in kb["inline_keyboard"]]
        self.assertEqual([b["text"] for b in buttons],
                         ["· 1 · MCP", "🔄 2 · testing", "· 3 · CV"])
        self.assertEqual([b["callback_data"] for b in buttons],
                         ["t:%s" % support.T3, "t:%s" % support.T1,
                          "t:%s" % support.T2])
        for b in buttons:
            self.assertLessEqual(len(b["callback_data"]), 64)

    async def test_all_tasks_includes_loose(self):
        txt, kb = self.cmd("/all_tasks")
        self.assertIn("All conversations", txt)
        self.assertIn("· 4. instalaciones · 📁default", txt)
        self.assertEqual(len(kb["inline_keyboard"]), 4)

    async def test_compact_lists_projects(self):
        r = self.cmd("/compact")
        txt, kb = r
        self.assertIn("🧹 <b>Compact — tap one</b> (idle only)", txt)
        self.assertIn("🔄 2. testing — 115.4K", txt)
        self.assertIn("· 1. MCP — 1.0K", txt)
        self.assertIn("· 3. CV — 132.2K", txt)
        self.assertEqual([f[0]["callback_data"].split(":")[0] for f in kb["inline_keyboard"]],
                         ["c", "c", "c"])

    async def test_all_compact_lists(self):
        txt, kb = self.cmd("/all_compact")
        self.assertIn("· 4. instalaciones", txt)
        self.assertEqual(len(kb["inline_keyboard"]), 4)

    async def test_stop_without_runs(self):
        r = self.cmd("/stop")
        self.assertEqual(
            r,
            "😌 Nothing running started from the phone.\n"
            "What runs on the Mac is stopped with the app's Stop button.")

    async def test_status(self):
        r = norm(self.cmd("/status"))
        self.assertIn("🎯 Active: «testing» · 📁bot", r)
        self.assertIn("🔄 «testing» since HH:MM", r)

    # ---------------- model / thinking ----------------

    async def test_model_default(self):
        txt, kb = self.cmd("/model")
        self.assertEqual(
            txt,
            "🎯 «testing»\n"
            "🤖 Model: GLM-5.3 · 🧠 thinking: high\n"
            "Tap to change (applies on the next turn):")
        buttons = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertEqual(buttons, ["● GLM-5.3", "○ GLM-5.3-Flash"])

    async def test_thinking_default(self):
        txt, kb = self.cmd("/thinking")
        self.assertEqual(
            txt,
            "🎯 «testing» · 🤖 GLM-5.3\n"
            "🧠 Thinking: high\n"
            "Tap to change (applies on the next turn):")
        buttons = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertEqual(buttons, ["○ max", "● high", "○ low"])

    async def test_model_set_preserves_thinking(self):
        r = self.cmd("/model GLM-5.3-Flash")
        self.assertIn("🤖 Model: GLM-5.3-Flash · 🧠 thinking: high", r)
        st = json.load(open(os.path.join(self.env_root, "hooks", "state",
                                         "bot-session.json")))
        self.assertEqual(st["model"]["modelId"], "GLM-5.3-Flash")
        self.assertEqual(st["model"]["options"]["reasoningLevel"], "high")

    async def test_thinking_set_preserves_model(self):
        r = self.cmd("/thinking max")
        self.assertIn("🧠 Thinking: max · model GLM-5.3", r)
        r2 = self.cmd("/thinking BAJA")  # invalid
        self.assertIn("⚠️ Levels: max · high · low", r2)

    async def test_mode_two_options_with_buttons(self):
        # F18: only Plan mode and Full access — nothing else
        txt, kb = self.cmd("/mode")
        self.assertIn("🎛 Mode: 🔓 Full access", txt)  # default: yolo
        rows = kb["inline_keyboard"]
        self.assertEqual(len(rows), 2)
        buttons = [b["text"] for row in rows for b in row]
        self.assertEqual(buttons, ["○ 🗺 Plan mode", "● 🔓 Full access"])
        cbs = [b["callback_data"] for row in rows for b in row]
        self.assertEqual(cbs, ["mo:plan", "mo:yolo"])

    async def test_mode_set_and_legacy(self):
        r = self.cmd("/mode plan")
        self.assertIn("🎛 Mode of «testing»: 🗺 Plan mode", r)
        st = json.load(open(os.path.join(self.env_root, "hooks", "state",
                                         "bot-session.json")))
        self.assertEqual(st["mode"], "plan")
        r2 = self.cmd("/mode guarded")  # the old 5: out
        self.assertIn("⚠️ Modes: plan · yolo", r2)

    # ---------------- task selection ----------------

    async def test_task_n_and_at(self):
        r = self.cmd("/task 3")  # projects: 1=MCP 2=testing 3=CV
        self.assertIn("🎯 Active conversation: «CV»", r)
        st = json.load(open(os.path.join(self.env_root, "hooks", "state",
                                         "bot-session.json")))
        self.assertEqual(st["sid"], support.T2)
        r2 = self.cmd("/task @%s" % support.T1)
        self.assertIn("🎯 Active conversation: «testing»", r2)

    async def test_task_number_out_of_list(self):
        r = self.cmd("/task 9")
        self.assertIn("⚠️ Number out of the list (1 to 3)", r)

    # ---------------- static info ----------------

    async def test_help_structure(self):
        r = self.cmd("/help")
        for frag in ("/task — with Project", "/all_tasks", "/compact — Projects list",
                     "/all_compact", "/stop — stops your phone run and CLEARS its message queue",
                     "/model — model of the active one · /thinking — max/high/low",
                     "/quota", "/usage", "/ctx", "/status", "/last"):
            self.assertIn(frag, r)

    async def test_start_and_ping(self):
        self.assertIn("🤖 ZCode control bot active", self.cmd("/start"))
        self.assertTrue(self.cmd("/ping"))

    async def test_last_chunks_and_sends(self):
        # /last returns None: it sends the chunks straight to Telegram —
        # we freeze the chunking by capturing the transport
        sent = []
        from unittest import mock
        from zcode_bot.utils import http as zc_http
        with mock.patch.object(zc_http, "send",
                               lambda cfg, text, loud=None, markup=None:
                               sent.append(text) or True):
            r = self.cmd("/last")
        self.assertIsNone(r)
        self.assertTrue(sent)
        self.assertIn("Sample last reply for /last.",
                      "".join(sent))

    async def test_usage_without_consumption(self):
        self.assertEqual(self.cmd("/usage"),
                         "📈 No usage recorded today yet.")

    async def test_ctx_report(self):
        r = self.cmd("/ctx")
        self.assertIn("🧠", r)
        self.assertIn("testing", r)
        # F31: the fixture's compact (now-120) has POSTERIOR usage
        # (now-60) → the real number is shown and NOT the «compacted»
        # notice (that case lives in test_timeouts_sesion.TestContextReport)
        self.assertIn("115.4K", r)
        self.assertNotIn("🧹", r)

    async def test_unknown_command(self):
        self.assertIsNone(self.cmd("/noexiste"))


if __name__ == "__main__":
    unittest.main()
