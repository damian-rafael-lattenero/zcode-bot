"""R5 — the callback table: ONE translation, covering the complete
historical if-chain (every branch of the old updates._cb_text, verbatim)
so that the move to commands/callbacks.py is a PROVEN equivalence, not
an assumed one. The t:ses_ old format is still pinned here; task:N was
removed (F54a) and its fall-through is pinned below.
"""
import os
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

from zcode_bot.commands import callbacks as cb  # noqa: E402
from zcode_bot import updates  # noqa: E402

SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"

# (callback_data, expected text) — one row per branch of the old if-chain
CASES = [
    ("t:%s" % SID, "/task @%s" % SID),
    ("t:ses_viejo", "/task @ses_viejo"),          # old short format
    ("s:%s" % SID, "/stop @%s" % SID),
    ("c:%s" % SID, "/compact @%s" % SID),
    ("mo:plan", "/mode plan"),
    ("qgo:%s:2" % SID, "/qgo %s:2" % SID),
    ("qdel:%s:0" % SID, "/qdel %s:0" % SID),
    ("retry", "/retry"),
    ("m:glm-5.3", "/model glm-5.3"),
    ("th:max", "/thinking max"),
    ("ans:look at this plan", "look at this plan"),  # textual reply
    ("yes", "yes"),
    ("si", "si"),                                  # P3b: bilingual forever  # noqa: spanish — legacy "si" value, bilingual forever
    ("no", "no"),
    ("nuc:go", "/nuclear go"),
    ("nuc:no", "/nuclear no"),
    ("cc", "/cotx"),                               # F56: reader's ✕
    ("dep:go", "/deploy go"),
    ("dep:no", "/deploy no"),
    ("df:2:abc1234", "/diff 2 abc1234"),
    ("dx:3:1", "/diff dirty 3 1"),
    ("dc:abc1234", "/diff commit abc1234"),
    ("dl", "/diff log"),
    ("dw", "/diff"),
    ("ct:%s:176:3:2" % SID, "/cotb %s 176 3 2" % SID),
]


class TestCallbackTable(unittest.TestCase):
    def test_every_historical_branch_translates_the_same(self):
        for data, expected in CASES:
            self.assertEqual(cb.callback_text(data), expected, data)
            # the historical updates alias is still THE same function
            self.assertEqual(updates._cb_text(data), expected, data)

    def test_unknown_and_planfix_fall_to_free_text(self):
        self.assertEqual(cb.callback_text("hello world" * 20),
                         ("hello world" * 20)[:64])
        # planfix (the «✏️ Add something to the plan» button): explicit passthrough
        self.assertEqual(cb.callback_text("planfix"), "planfix")

    def test_is_question_reply(self):
        self.assertTrue(cb.is_question_reply("ans:x"))
        self.assertTrue(cb.is_question_reply("yes"))
        self.assertTrue(cb.is_question_reply("si"))  # P3b: both, forever  # noqa: spanish — legacy "si" value, bilingual forever
        self.assertTrue(cb.is_question_reply("no"))
        self.assertFalse(cb.is_question_reply("retry"))
        self.assertFalse(cb.is_question_reply(""))
        self.assertFalse(cb.is_question_reply("no:false"))

    def test_old_task_position_vocabulary_falls_through(self):
        # F54a: "task:" was removed from PREFIXES — a stale pre-R5 button
        # tap falls to the default (free text) instead of translating to
        # /all_tasks N, where N could point at the WRONG conversation.
        self.assertEqual(cb.callback_text("task:3"), "task:3")


if __name__ == "__main__":
    unittest.main()
