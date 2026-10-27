"""F79b — the canonical logging engine.

One BotFormatter owns the record shape (the plan's chosen format,
key=value greppable, human):
[2026-10-27 14:25:48.123 -03] ERROR zcode_bot.errors component=… sid=… turn=… cause=KeyError: 'ph'
the message
    Traceback (most recent call last):
      …
Contract tests: exact header anatomy, ambient context via contextvars
(bind once at the entrypoint — every record carries it), cause= whenever
an exception rides the record, the stack indented and NEVER truncated,
BotError context keys, and rotation keeping the one-generation rule.
pwlog stays OUT of the engine (frozen forensic channel)."""
import os
import re
import time
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot import config
from zcode_bot.utils import logging as zlog


def _read(kind="errors"):
    with open(os.path.join(config.LOG_DIR, "%s.log" % kind)) as f:
        return f.read()


class TestBotFormatter(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        zlog._reset_handlers()

    def tearDown(self):
        zlog._reset_handlers()
        zlog.bind_context(sid="", turn="", component="")

    def test_header_anatomy_ts_ms_tz_level_logger(self):
        zlog.log("errors", "plain line")
        line = _read().splitlines()[0]
        # [2026-10-27 14:25:48.123 -03] INFO zcode_bot.errors plain… no:
        # the header line ends at the logger name; the message is line 2
        m = re.match(
            r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.(\d{3}) ([+-]\d{2,4})\] "
            r"INFO zcode_bot\.errors$", line)
        self.assertIsNotNone(m, line)
        when, ms, tz = m.groups()
        self.assertTrue(0 <= int(ms) <= 999)
        # the ts is now (the record's own clock, not the write's)
        self.assertIn(time.strftime("%Y-%m-%d"), line, "ts not from today: %s" % line)
        self.assertEqual(_read().splitlines()[1], "plain line")

    def test_context_rides_every_record_once_bound(self):
        prev = zlog.bind_context(sid="b14e2541", turn="7ef4f6ee",
                                 component="cot-live")
        self.assertEqual(prev, {"sid": "", "turn": "", "component": ""})
        zlog.log("errors", "with context")
        head = _read().splitlines()[0]
        self.assertIn("component=cot-live", head)
        self.assertIn("sid=b14e2541", head)
        self.assertIn("turn=7ef4f6ee", head)
        zlog.bind_context(**prev)  # restore: next records go bare
        zlog.log("errors", "bare again")
        self.assertNotIn("component=", _read().splitlines()[2])

    def test_cause_and_indented_stack_on_exceptions(self):
        try:
            raise KeyError("ph")
        except KeyError:
            zlog.log_exc("errors", "the wall fired")
        txt = _read()
        head, body = txt.splitlines()[0], txt.splitlines()[1]
        self.assertIn("ERROR", head)
        self.assertIn("cause=KeyError: 'ph'", head)
        self.assertEqual(body, "the wall fired")
        self.assertIn("    Traceback (most recent call last)", txt)
        self.assertIn("    KeyError: 'ph'", txt)

    def test_the_stack_is_never_truncated(self):
        # the [-500:] amputation cut the deepest frames; 40 frames deep
        # must arrive COMPLETE — 3.13 collapses repeated recursion with
        # a "[Previous line repeated N more times]" marker, which is a
        # rendering, not a truncation: bottom + marker + outer frame all
        # present
        def deep(n):
            if n == 0:
                raise RuntimeError("bottom-f79b")
            deep(n - 1)
        try:
            deep(40)
        except RuntimeError:
            zlog.log_exc("errors", "deep wall")
        txt = _read()
        self.assertIn("    RuntimeError: bottom-f79b", txt)
        self.assertIn("Traceback (most recent call last)", txt)
        self.assertIn("[Previous line repeated", txt)
        self.assertIn("    raise RuntimeError", txt)

    def test_boterror_context_keys_in_the_header(self):
        try:
            try:
                raise OSError("socket gone")
            except OSError as e:
                raise zlog.AttachError("respawn failed",
                                       sid="abcd1234",
                                       watcher="cot-live") from e
        except zlog.AttachError:
            zlog.log_exc("errors", "attach wall")
        head = _read().splitlines()[0]
        self.assertIn('cause=AttachError: respawn failed', head)
        self.assertIn("bot_ctx=", head)
        self.assertIn("abcd1234", head)
        # the original cause survives the chain
        self.assertIn("OSError: socket gone", _read())


class TestEngineMechanics(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        zlog._reset_handlers()

    def tearDown(self):
        zlog._reset_handlers()
        zlog.bind_context(sid="", turn="", component="")

    def test_handler_reopens_when_log_dir_moves(self):
        # fixtures rebind config.LOG_DIR per test — a stale handler would
        # keep writing into the previous (deleted) tree
        zlog.log("errors", "first dir")
        first = _read()
        self.assertIn("first dir", first)
        old_dir = config.LOG_DIR
        config.LOG_DIR = os.path.join(old_dir, "moved")
        try:
            zlog.log("errors", "second dir")
            with open(os.path.join(config.LOG_DIR, "errors.log")) as f:
                self.assertIn("second dir", f.read())
        finally:
            config.LOG_DIR = old_dir

    def test_log_never_raises_on_a_broken_log_dir(self):
        real = config.LOG_DIR
        config.LOG_DIR = os.path.join(real, "no", "such", "dir")
        try:
            zlog.log("errors", "into the void")   # must not raise
            zlog.log_exc("errors", "void wall")  # neither
        finally:
            config.LOG_DIR = real

    def test_pwlog_is_not_the_engine(self):
        # the forensic channel keeps its frozen format: [MM-DD HH:MM:SS] …
        zlog.pwlog("kb", "step n=1")
        p = os.path.join(config.BASE, "hooks", "logs", "prompt-worker.log")
        with open(p) as f:
            line = f.read().strip().splitlines()[-1]
        self.assertTrue(re.match(r"^\[\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] kb step n=1$",
                                 line), line)

    def test_rotation_one_generation(self):
        zlog._ROT_BYTES = 2048
        try:
            for i in range(40):
                zlog.log("errors", "filler line number %03d " % i * 8)
            base = os.path.join(config.LOG_DIR, "errors.log")
            self.assertTrue(os.path.exists(base + ".1"))
            self.assertFalse(os.path.exists(base + ".2"))
        finally:
            zlog._ROT_BYTES = 5 * 1024 * 1024


if __name__ == "__main__":
    unittest.main()
