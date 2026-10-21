"""Tests for PURE logic (no state, no network): parsing, classification,
button callbacks, formatting. They freeze the helpers' contract.
"""
import unittest

import support


class PureHelpers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    # ---------------- _cb_text: callback vocabulary ----------------

    async def test_cb_text_vocabulary(self):
        cb = self.zt._cb_text
        self.assertEqual(cb("t:sess_abc-1"), "/task @sess_abc-1")
        self.assertEqual(cb("t:ses_f02xyz"), "/task @ses_f02xyz")  # old format
        self.assertEqual(cb("c:sess_abc"), "/compact @sess_abc")
        self.assertEqual(cb("c:ses_f02xyz"), "/compact @ses_f02xyz")
        self.assertEqual(cb("s:sess_abc"), "/stop @sess_abc")
        self.assertEqual(cb("m:GLM-5.3-Flash"), "/model GLM-5.3-Flash")
        self.assertEqual(cb("mo:plan"), "/mode plan")
        self.assertEqual(cb("mo:yolo"), "/mode yolo")
        self.assertEqual(cb("th:medium"), "/thinking medium")
        self.assertEqual(cb("task:2"), "/all_tasks 2")   # old buttons → all
        self.assertEqual(cb("ans:3"), "3")
        self.assertEqual(cb("yes"), "yes")
        self.assertEqual(cb("no"), "no")

    # ---------------- yes/no classification ----------------

    async def test_classify(self):
        cl = self.zt.classify
        self.assertEqual(cl("si"), "yes")  # noqa: spanish — bilingual classify input, permanent
        self.assertEqual(cl("SÍ, dale"), "yes")  # noqa: spanish — bilingual classify input, permanent
        self.assertEqual(cl("aprobalo"), "yes")
        self.assertEqual(cl("no"), "no")
        self.assertEqual(cl("denegado"), "no")
        self.assertEqual(cl("hello how are you"), "text")
        self.assertEqual(cl(""), "text")

    async def test_norm(self):
        n = self.zt.norm
        self.assertEqual(n("  HËLLÒ   WÔRLD "), "hello world")

    # ---------------- formatting ----------------

    async def test_fmt_tok(self):
        f = self.zt.fmt_tok
        self.assertEqual(f(999), "999")
        self.assertEqual(f(15800), "15.8K")
        self.assertEqual(f(17400000), "17.4M")
        self.assertEqual(f(None), "?")

    # ---------------- _compact_args: parser (zcode_compact faked) ----------------

    async def test_compact_args_parser(self):
        # F4f: _compact_args/zcode_compact live in reporting — the spy
        # goes to the owning module (a rebind in gateway would be orphaned)
        from unittest import mock
        from zcode_bot import reporting as zc_reporting
        calls = []
        with mock.patch.object(
                zc_reporting, "zcode_compact",
                lambda rest="", n=None, sid=None, scope=None:
                    calls.append((rest, n, sid, scope)) or "ok"):
            ca = self.zt._compact_args
            ca("3", "projects")
            ca("foco en X", "all")
            ca("@sess_zz9", "all")
            ca("@ses_aa1 instr extra", "projects")
        self.assertEqual(calls[0], ("", 3, None, "projects"))
        self.assertEqual(calls[1], ("foco en X", None, None, "all"))
        self.assertEqual(calls[2], ("", None, "sess_zz9", "all"))
        self.assertEqual(calls[3], ("instr extra", None, "ses_aa1", "projects"))

    # ---------------- _cur_sel / _set_sel ----------------

    async def test_cur_sel_sanitizes(self):
        cur = self.zt._cur_sel({})
        self.assertEqual(cur["modelId"], "GLM-5.3")
        self.assertEqual(cur["options"]["reasoningLevel"], "high")
        cur2 = self.zt._cur_sel({"model": {"modelId": "GLM-5.3-Flash"}})
        self.assertEqual(cur2["modelId"], "GLM-5.3-Flash")
        self.assertEqual(cur2["options"]["reasoningLevel"], "high")  # no options given

    async def test_set_sel_mutual_preservation(self):
        # the real flow re-reads the active session between commands: same here
        self.zt.set_active_bot_session({"sid": "s1", "title": "t", "ws": "/w"})
        self.zt._set_sel(self.zt.active_bot_session(), model_id="GLM-5.3-Flash")
        self.zt._set_sel(self.zt.active_bot_session(), thinking="max")
        cur = self.zt._cur_sel(self.zt.active_bot_session())
        self.assertEqual(cur["modelId"], "GLM-5.3-Flash")
        self.assertEqual(cur["options"]["reasoningLevel"], "max")

    # ---------------- extract assistant text ----------------

    async def test_extract_assistant_text(self):
        msgs = [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "q"}]},
            {"info": {"role": "assistant"},
             "parts": [{"type": "reasoning", "text": "thought"},
                       {"type": "text", "text": "reply"},
                       {"type": "tool_use", "name": "Bash"}]},
            {"info": {"role": "assistant"},
             "parts": [{"type": "text", "text": "second"}]},
        ]
        self.assertEqual(self.zt._extract_assistant_text(msgs),
                         "reply\n\nsecond")
        self.assertEqual(self.zt._extract_assistant_text(msgs, last_only=True),
                         "second")


if __name__ == "__main__":
    unittest.main()
