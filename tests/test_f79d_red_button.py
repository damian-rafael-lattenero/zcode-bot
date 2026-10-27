"""F79d — the complete red button.

/nuclear now kills the FULL watcher stack (agent-live/agent-view died
with no cycle before — independent processes streaming into a restarted
world), /status and /handshake carry the channel's health line (the
heartbeat ages + the F79c contract verdict — the question «is the
channel alive?» stops requiring forensics), bot-revive exists on the
Mac for the day Telegram itself is the corpse, and the dumb launchd
healer steps aside while a REAL nuclear cycle owns the poller."""
import os
import time
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot import config
from zcode_bot import healer
from zcode_bot.core import set_active_bot_session

SID = "session_b14e25588934bcdfe719e3724e90411b"


def _touch(path, age=0.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a"):
        os.utime(path, None)
    if age:
        os.utime(path, (time.time() - age,) * 2)


class TestHealthLine(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        healer.reset_bookkeeping()

    def tearDown(self):
        healer.reset_bookkeeping()

    def test_line_carries_ages_and_verdict(self):
        set_active_bot_session({"sid": SID, "focus_sid": SID,
                                "title": "t", "ws": "/w"})
        _touch(os.path.join(config.STATE_DIR, "hb-transport"), age=3)
        _touch(config.marker_path(SID))                     # turn running
        _touch(os.path.join(config.STATE_DIR, "cot-%s-1.json" % SID), age=5)
        _touch(os.path.join(config.STATE_DIR, "cot-live-%s.pid" % SID), age=2)
        pw = os.path.join(config.BASE, "hooks", "logs", "prompt-worker.log")
        _touch(pw, age=4)
        line = healer.health_line()
        self.assertIn("Channel:", line)
        self.assertIn("transport", line)
        self.assertIn("cot-live", line)
        self.assertIn("contract: ok", line)

    def test_no_focus_says_so(self):
        set_active_bot_session({"sid": "", "focus_sid": "", "ws": ""})
        line = healer.health_line()
        self.assertIn("contract: no focus", line)

    def test_status_and_handshake_carry_the_line(self):
        # /status
        from zcode_bot.commands.handlers_info import _cmd_status
        self.assertIn("Channel:", _cmd_status({}, ""))
        # /handshake
        from zcode_bot.commands.handlers_system import _cmd_handshake
        self.assertIn("Channel:", _cmd_handshake(
            {"chat_id": 1, "token": "T", "owner_user_id": 9,
             "enabled": True}, ""))


class TestNuclearFullStack(unittest.TestCase):
    """The kill-off now covers the agent watchers (F79d)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_agent_watchers_die_with_the_cycle(self):
        from zcode_bot import agent_live
        from zcode_bot import nuclear
        # a live agent-live watcher (pid-file, cmdline-verified)
        _touch(os.path.join(config.STATE_DIR, "agent-live-%s.pid" % SID))
        with open(os.path.join(config.STATE_DIR,
                               "agent-live-%s.pid" % SID), "w") as f:
            f.write("424242")
        _touch(os.path.join(config.STATE_DIR, "agent-view-%s.pid" % SID))
        with open(os.path.join(config.STATE_DIR,
                               "agent-view-%s.pid" % SID), "w") as f:
            f.write("424243")
        pids = agent_live.agent_watcher_pids()
        self.assertIn(424242, pids)
        self.assertIn(424243, pids)
        killed = []
        import signal

        class _FakeProc:  # os.kill spy through run? use the real os.kill seam
            pass

        real_kill = os.kill
        def fake_kill(pid, sig):
            if sig == signal.SIGTERM:
                killed.append(pid)
                raise OSError("gone")   # pretend it died instantly
            raise OSError("gone")
        os.kill = fake_kill
        try:
            nuclear._nuc_kill_own_runtimes(
                run=lambda *a, **k: None,
                is_cot_live=lambda pid: False,
                agent_pids=lambda: [424242, 424243],
                is_agent=lambda pid: True)
        finally:
            os.kill = real_kill
        self.assertIn(424242, killed)
        self.assertIn(424243, killed)
        # the pid-files were cleaned: the respawn's single-flight is free
        self.assertFalse(os.path.exists(
            os.path.join(config.STATE_DIR, "agent-live-%s.pid" % SID)))
        self.assertFalse(os.path.exists(
            os.path.join(config.STATE_DIR, "agent-view-%s.pid" % SID)))


class TestTheMacSideButton(unittest.TestCase):
    """bot-revive + bot-healer: the scripts that live OUTSIDE the
    package (they must run when the package is the broken thing)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_bot_revive_script_exists_and_is_executable(self):
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        p = os.path.join(repo, "scripts", "bot-revive.sh")
        self.assertTrue(os.path.exists(p))
        self.assertTrue(os.access(p, os.X_OK))

    def test_bot_healer_steps_aside_during_nuclear(self):
        # the dumb agent reads ONE more mtime: a fresh bot-nuclear flag
        # means a real cycle owns the poller — kickstarting it would
        # fight the cycle
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        src = open(os.path.join(repo, "scripts", "bot-healer.py")).read()
        self.assertIn("bot-nuclear", src)
        self.assertIn("NUCLEAR_GRACE_S", src)
        # and it stays dumb: zero imports from the package
        self.assertNotIn("import zcode_bot", src)

    def test_deploy_installs_both(self):
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        src = open(os.path.join(repo, "deploy.sh")).read()
        self.assertIn("bot-revive", src)
        self.assertIn("com.zcode.bot-healer.plist", src)


if __name__ == "__main__":
    unittest.main()
