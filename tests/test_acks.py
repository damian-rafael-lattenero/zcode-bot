"""F36 — acks: the «choice received» ack component.

A button's cycle has two feedbacks: the native 👀 (Telegram took it)
and this component («the app ACCEPTED the request»). One single format
for compact (F35), stop and reply-to-agent — the compact tests live in
test_timeouts_sesion (fully faked flow); this file holds the pure
component's contract.
"""
import os
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

from zcode_bot import acks  # noqa: E402

CFG = {"token": "T", "chat_id": 1}


class TestChoiceReceived(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.p = mock.patch.object(acks.uhttp, "send",
                                   lambda cfg, text, loud=None, markup=None, a_outbox=True:
                                   (self.sends.append(text), True)[1])
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_format_with_detail(self):
        acks.choice_received(CFG, "compacting «CV»",
                                 "I'll ping you when it finishes")
        self.assertEqual(self.sends,
                         ["📬 Your choice arrived: compacting «CV»"
                          " — I'll ping you when it finishes"])

    def test_format_without_detail(self):
        acks.choice_received(CFG, "stopped «testing»")
        self.assertEqual(self.sends, ["📬 Your choice arrived: stopped «testing»"])

    def test_escapes_html_of_what(self):
        acks.choice_received(CFG, "stopped «a<b»")
        self.assertIn("a&lt;b", self.sends[0])
        self.assertNotIn("a<b", self.sends[0])

    def test_always_silent(self):
        """The buzz belongs to the RESULT: the ack travels with loud=False."""
        captured = {}
        with mock.patch.object(acks.uhttp, "send",
                               lambda cfg, text, loud=None, markup=None, a_outbox=True:
                                   captured.update(loud=loud)):
            acks.choice_received(CFG, "x")
        self.assertFalse(captured["loud"])


if __name__ == "__main__":
    unittest.main()
