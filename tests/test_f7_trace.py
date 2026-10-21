"""F7: turn trace — step by step + full CoT over Telegram.

Freezes: steps/reasoning extractors from the history, sendDocument's
multipart body, build_trace, _turn_trace (persistence + flag) and the
presence of /cot in registration/menu/help.
"""
import os
import sys
import unittest

import support
from zcode_bot.live_view import COT_CHUNK as CH  # noqa: E402
  # inserts src/ into sys.path (collection determinism)
from zcode_bot import config as zc_config  # noqa: E402


def _msg(role, parts):
    return {"info": {"role": role, "id": "m%d" % int(abs(hash(str(parts))) % 10**8)},
            "parts": parts}


MSGS = [
    _msg("user", [{"type": "text", "text": "fix it"}]),
    _msg("assistant", [
        {"type": "step-start"},
        {"type": "reasoning", "text": "I think I'll verify the file."},
        {"type": "tool", "tool": "Read", "state": {"input": {"file_path": "/a/b.py"}}},
    ]),
    _msg("assistant", [
        {"type": "reasoning", "text": "Now I run the tests."},
        {"type": "tool", "tool": "Bash",
         "state": {"input": {"command": ["python3", "-m", "unittest", "-q"]}}},
        {"type": "tool", "tool": "Edit",
         "state": {"input": {"file_path": "/a/b.py"}}},
    ]),
    _msg("assistant", [
        {"type": "text", "text": "Done, fixed."},
    ]),
]


class TestCotChunking(unittest.IsolatedAsyncioTestCase):
    """Chunker of the incremental CoT: permanent 3000-char boxes while
    the live queue retains the latest (Telegram's 4096 limit)."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_below_threshold_sends_nothing(self):
        out, flushed = self.zt._cot_chunks_to_send("x" * 100, 0)
        self.assertEqual(out, [])
        self.assertEqual(flushed, 0)

    async def test_excess_is_chunked_in_3000(self):
        buf = "".join(str(i % 10) for i in range(10000))
        out, flushed = self.zt._cot_chunks_to_send(buf, 0)
        self.assertEqual([len(t) for t in out], [CH, CH, CH])
        self.assertEqual(flushed, 9000)
        self.assertEqual(out[0], buf[:CH])
        self.assertEqual(out[2], buf[6000:9000])

    async def test_without_cap_all_are_sent(self):
        """2026-10-13: Damian asked for CoT with NO cap — 200K chars =
        66 boxes of 3000 (the live queue keeps the last ~2000)."""
        buf = "y" * 200000
        out, flushed = self.zt._cot_chunks_to_send(buf, 0)
        self.assertEqual(len(out), 66)
        self.assertEqual(flushed, 66 * CH)
        self.assertEqual("".join(out), buf[:flushed])

    async def test_final_flush_takes_everything(self):
        buf = "z" * 5000
        out, flushed = self.zt._cot_chunks_to_send(buf, 0, tail_keep=0)
        self.assertEqual(flushed, 5000)            # no queue: the whole rest
        self.assertEqual("".join(out), buf)


class TestExtractores(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_tool_steps_with_command_and_file(self):
        steps = self.zt._steps_of_messages(MSGS)
        self.assertEqual(steps, [
            ("Read", "/a/b.py"),
            ("Bash", "python3 -m unittest -q"),
            ("Edit", "/a/b.py"),
        ])

    async def test_steps_tolerant_without_input(self):
        steps = self.zt._steps_of_messages(
            [_msg("assistant", [{"type": "tool", "tool": "Grep", "state": {}}])])
        self.assertEqual(steps, [("Grep", "")])

    async def test_reasoning_only_assistant_and_in_order(self):
        cot = self.zt._reasoning_of_messages(MSGS)
        self.assertIn("verify the file", cot)
        self.assertIn("Now I run the tests", cot)
        self.assertLess(cot.index("verify"), cot.index("Now I run"))
        # the answer text is NOT reasoning
        self.assertNotIn("Done, fixed", cot)

    async def test_reasoning_empty_when_absent(self):
        self.assertEqual(self.zt._reasoning_of_messages(
            [_msg("assistant", [{"type": "text", "text": "text only"}])]), "")


class TestMultipart(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_body_has_filename_and_content_fields(self):
        from zcode_bot.utils.http import _multipart_body
        body, ctype = _multipart_body(
            {"chat_id": "42", "caption": "hello"}, "cot-x.txt", b"LINE1\nLINE2")
        self.assertIn("multipart/form-data; boundary=", ctype)
        raw = body.decode()
        self.assertIn('name="chat_id"', raw)
        self.assertIn("42", raw)
        self.assertIn('filename="cot-x.txt"', raw)
        self.assertIn("LINE1", raw)
        self.assertTrue(raw.rstrip().endswith("--"))


class TestSendDocument(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_send_document_posts_multipart_to_the_bot(self):
        import urllib.request as _u
        capturado = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            capturado["url"] = req.full_url
            capturado["data"] = req.data
            capturado["ctype"] = req.headers.get("Content-type")
            return FakeResp()

        support.patch(self, _u, "urlopen", fake_urlopen)  # R11: cleanup
        ok = self.zt.send_document({"enabled": True, "token": "T", "chat_id": 7},
                                   "turn-x.txt", "contenido", "cap")
        self.assertTrue(ok)
        self.assertIn("/botT/sendDocument", capturado["url"])
        self.assertIn("multipart/form-data", capturado["ctype"])
        self.assertIn(b"filename=\"turn-x.txt\"", capturado["data"])

    async def test_failure_does_not_blow_up_and_returns_false(self):
        import urllib.request as _u

        def fake_urlopen(req, timeout=None):
            raise OSError("red caida")

        support.patch(self, _u, "urlopen", fake_urlopen)  # R11: cleanup
        ok = self.zt.send_document({"token": "T", "chat_id": 7},
                                   "x.txt", "x")
        self.assertFalse(ok)


class TestBuildTrace(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_complete_trace_with_sections(self):
        t = self.zt.build_trace("Mi task", "ses_x", 1.5, MSGS)
        self.assertIn("TURN: Mi task", t)
        self.assertIn("STEPS (3)", t)
        self.assertIn("1. Read · /a/b.py", t)
        self.assertIn("2. Bash · python3 -m unittest -q", t)
        self.assertIn("FULL REASONING", t)
        self.assertIn("verify the file", t)
        self.assertIn("REPLY", t)
        self.assertIn("Done, fixed", t)


class TestTurnTrace(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_default_does_not_send_but_persists(self):
        # F19: the document stopped being sent on its own (redundant noise
        # with the CoT boxes) — default OFF, the state file always remains
        sent = []
        support.patch(self, self.zt, "send_document",
                        lambda *a, **k: sent.append(1))
        self.zt._turn_trace({}, "T1", "ses_1", 2.0, MSGS)
        self.assertEqual(sent, [])
        path = os.path.join(zc_config.STATE_DIR, "last-turn-trace.txt")
        self.assertTrue(os.path.exists(path))

    async def test_flag_on_sends_document(self):
        # F4f: _turn_trace lives in reporting and uses uhttp.send_document —
        # the stub goes to the transport seam, not the gateway's namespace
        from unittest import mock
        from zcode_bot.utils import http as zc_http
        sent = []
        with mock.patch.object(
                zc_http, "send_document",
                lambda cfg, fname, text, caption="":
                    (sent.append((fname, text, caption)), True)[1]):
            self.zt._turn_trace({"trace_documents": True}, "T1", "ses_1",
                                2.0, MSGS)
        path = os.path.join(zc_config.STATE_DIR, "last-turn-trace.txt")
        with open(path) as f:
            self.assertIn("STEPS (3)", f.read())
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].startswith("turn-T1-"))
        self.assertIn("3 steps", sent[0][2])


class TestCotComando(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_without_active_notifies(self):
        self.zt.set_active_bot_session({})
        r = self.zt._cmd_cot({}, "")
        self.assertIn("Pick a conversation first", r)

    async def test_invalid_sid_notifies(self):
        r = self.zt._cmd_cot({}, "@ses_inexistente_xyz")
        self.assertIn("couldn't find that conversation", r)

    async def test_register_menu_and_help(self):
        self.assertIsNotNone(self.zt.handle_command({}, "/cot"))
        self.assertIn("/cot", self.zt._cmd_help({}, ""))
        cap = {}

        fake_tg_api = support.make_fake_tg_api(cap)

        from zcode_bot.utils import http as zc_http
        support.patch(self, zc_http, "tg_api", fake_tg_api)
        cfg = self.zt.load_config()
        cfg["chat_id"] = 1
        self.assertTrue(self.zt.register_bot_menu(cfg))
        names = [c["command"] for c in cap["setMyCommands"]["commands"]]
        self.assertIn("cot", names)


if __name__ == "__main__":
    unittest.main()
