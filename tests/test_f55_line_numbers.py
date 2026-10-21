"""F55: line numbers on every file sent to the Telegram chat.

Freezes: number_lines (the pure gutter), the sendDocument seam (the
multipart body goes out numbered) and the on-disk trace staying RAW
(it is state, not chat).
"""
import os
import unittest
import urllib.request as _u

import support
from zcode_bot import config as zc_config
from zcode_bot.utils import http as zc_http
from zcode_bot.utils.formatting import number_lines


class TestNumberLines(unittest.TestCase):
    def test_every_line_numbered_right_aligned(self):
        self.assertEqual(number_lines("a\nb\nc"),
                         "1 │ a\n2 │ b\n3 │ c")

    def test_width_grows_past_nine_lines(self):
        text = "\n".join("l%d" % i for i in range(1, 11))
        self.assertIn("\n 9 │ l9\n10 │ l10", number_lines(text))

    def test_empty_lines_are_numbered_too(self):
        self.assertEqual(number_lines("a\n\nb"), "1 │ a\n2 │ \n3 │ b")

    def test_trailing_newline_is_not_a_phantom_line(self):
        self.assertEqual(number_lines("a\n"), "1 │ a")
        self.assertEqual(number_lines("a\n\n"), "1 │ a\n2 │ ")

    def test_empty_text_stays_empty(self):
        self.assertEqual(number_lines(""), "")

    def test_unicode_content_passes_through_untouched(self):
        self.assertEqual(number_lines("🟢 日本語 ✓"),
                         "1 │ 🟢 日本語 ✓")


class TestSeamNumbering(unittest.TestCase):
    """send_document is THE seam: whatever the producer, the file that
    reaches the chat carries its line numbers."""

    def setUp(self):
        self.zt = support.fresh(self)

    def _capture(self, text):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data
            return FakeResp()

        support.patch(self, _u, "urlopen", fake_urlopen)  # R11: cleanup
        ok = zc_http.send_document({"enabled": True, "token": "T",
                                    "chat_id": 7},
                                   "turn-x.txt", text, "cap")
        self.assertTrue(ok)
        return captured["data"]

    def test_body_goes_out_numbered(self):
        self.assertIn("1 │ first\n2 │ second".encode("utf-8"),
                      self._capture("first\nsecond"))

    def test_body_of_a_one_liner(self):
        self.assertIn("1 │ solo".encode("utf-8"), self._capture("solo"))

    def test_persisted_trace_stays_raw(self):
        """The state/last-turn-trace.txt copy is NOT chat: no gutter."""
        sent = []
        support.patch(self, zc_http, "send_document",
                      lambda cfg, fname, text, caption="":
                      (sent.append(text), True)[1])
        self.zt._turn_trace({"trace_documents": True}, "T1", "ses_1",
                            2.0, [])
        path = os.path.join(zc_config.STATE_DIR, zc_config.LAST_TRACE_FILE)
        with open(path) as f:
            persisted = f.read()
        self.assertNotIn(" │ ", persisted)
        self.assertTrue(persisted.startswith("╔═ TURN:"))
        # what the producer handed to the seam was the raw trace too —
        # the gutter belongs to the transport, not the document
        self.assertNotIn(" │ ", sent[0])


if __name__ == "__main__":
    unittest.main()
