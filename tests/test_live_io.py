# -*- coding: utf-8 -*-
"""live_io — the reader of the fork's live-CoT sidecar (F73).

The fork flushes mid-request reasoning to live-io-<session>.jsonl (one
line per throttle window, tails capped, removed at completion). This is
the bot's pure-file door to REAL in-flight thinking — tested with plain
files, no runtime.
"""
import datetime
import json
import os
import time
import unittest

import support  # noqa: E402
from zcode_bot import live_io  # noqa: E402

SID = "sess_live_1"


def _line(chars=1500, tail="thinking about the invariant", age_s=0.0,
          sid=SID, text_chars=0):
    return json.dumps({
        "type": "live_io_partial", "sessionId": sid,
        "querySource": "main_turn", "turnId": "t1", "requestId": "r1",
        "attempt": 1, "reasoningChars": chars, "textChars": text_chars,
        "reasoningTail": tail, "textTail": "",
        "updatedAt": datetime.datetime.fromtimestamp(
            time.time() - age_s,
            tz=datetime.timezone.utc).isoformat()}) + "\n"


class TestLiveIo(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _write(self, *lines):
        with open(live_io.sidecar_path(SID), "w") as f:
            f.write("".join(lines))

    def test_the_sidecar_sits_next_to_the_rollout(self):
        self.assertTrue(live_io.sidecar_path(SID).endswith(
            "live-io-%s.jsonl" % SID))
        self.assertIn("rollout", live_io.sidecar_path(SID))
        self.assertNotIn("model-io", live_io.sidecar_path(SID))

    def test_latest_reads_the_last_complete_line(self):
        self._write(_line(chars=100, tail="first"),
                    _line(chars=4200, tail="second, the live one"))
        d = live_io.latest(SID)
        self.assertEqual(d["reasoningChars"], 4200)
        self.assertIn("second", d["reasoningTail"])

    def test_latest_none_without_file_or_stale_or_foreign(self):
        self.assertIsNone(live_io.latest(SID))            # no sidecar
        self._write(_line(age_s=3600))                    # a crash corpse
        self.assertIsNone(live_io.latest(SID))
        self._write(_line())
        self.assertEqual(live_io.latest(SID)["type"],
                         "live_io_partial")
        self._write(_line(chars=0, tail="").rstrip("\n"))  # mid-write line
        self.assertIsNone(live_io.latest(SID))

    def test_chars_line_and_tail_of(self):
        self.assertEqual(live_io.chars_line({"reasoningChars": 4200}),
                         "in-flight ~4k chars")
        self.assertEqual(live_io.chars_line({"reasoningChars": 700,
                                             "textChars": 30}),
                         "in-flight 700 chars")
        self.assertEqual(live_io.chars_line({}), "")
        self.assertEqual(len(live_io.tail_of(
            {"reasoningTail": "x" * 9000}, cap=600)), 600)
        self.assertTrue(lio_ends(live_io.tail_of(
            {"reasoningTail": "abc" * 10}, cap=5), "bcabc"))


def lio_ends(s, suf):
    return s.endswith(suf)


if __name__ == "__main__":
    unittest.main()
