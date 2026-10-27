"""F79c — the healer: the channel's watchdog of contract.

The scenario this module exists for is TODAY's, verbatim: a turn ran on
the Mac (14:21-14:25), the CoT capture kept producing, and delivery
froze at 14:25:48 with the transport alive — a dead-in-function channel
that every pre-F79 signal missed. The contract under test: turn running
+ fresh content (cache or sidecar) + delivery silent (kb and/or watcher
beat stale) → heal. The ladder: 1 = silent re-attach (F78c's 📥 bundle
reports the landing), 2 = re-attach + 💀 report, 3+ = ☢️ /nuclear
advice, one episode per cooldown, never an infinite respawn loop."""
import os
import time
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot import config
from zcode_bot import healer
from zcode_bot.core import set_active_bot_session
from zcode_bot.healer import Obs, evaluate

SID = "session_b14e25588934bcdfe719e3724e90411b"


def _touch(path, age=0.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a"):
        os.utime(path, None)
    if age:
        os.utime(path, (time.time() - age,) * 2)


class TestContract(unittest.TestCase):
    """The pure evaluator: mtimes → verdict. Every row is one real
    moment of 27/10."""

    def test_no_turn_running_no_heal(self):
        self.assertEqual(
            evaluate(Obs(turn_running=False, cache_age=5, kb_age=300)), "no_turn")

    def test_quiet_with_nothing_owed_is_healthy(self):
        # the turn thinks but the store has nothing NEW to deliver
        self.assertEqual(
            evaluate(Obs(turn_running=True, cache_age=400, kb_age=400)), "ok")

    def test_the_142548_signature_is_dead_channel(self):
        # content fresh + kb silent (today's exact forensics)
        self.assertEqual(
            evaluate(Obs(turn_running=True, cache_age=10, kb_age=200)), "dead_channel")

    def test_stale_watcher_beat_is_dead_channel_even_if_kb_fresh(self):
        # the kb may tick for OTHER sessions — this session's watcher beat
        # is the session-scoped truth
        self.assertEqual(
            evaluate(Obs(turn_running=True, cache_age=10, kb_age=5,
                         watcher_age=400)), "dead_channel")

    def test_fresh_everything_is_healthy(self):
        self.assertEqual(
            evaluate(Obs(turn_running=True, cache_age=10, kb_age=5,
                         watcher_age=3)), "ok")

    def test_sidecar_counts_as_content(self):
        # the fork's live-io (the runtime's own flush) is as good a
        # "something to deliver" as the cache
        self.assertEqual(
            evaluate(Obs(turn_running=True, cache_age=None,
                         sidecar_age=5, kb_age=200)), "dead_channel")


class TestObserve(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_observe_reads_the_real_files(self):
        # the 14:25:48 scene, materially: marker + fresh cache + silent
        # kb + stale watcher pid
        _touch(config.marker_path(SID), age=0)            # turn running
        _touch(os.path.join(config.STATE_DIR,
                            "cot-%s-1793121614.json" % SID), age=5)
        _touch(os.path.join(config.STATE_DIR, "cot-live-%s.pid" % SID),
               age=400)
        pw = os.path.join(config.BASE, "hooks", "logs", "prompt-worker.log")
        _touch(pw, age=200)
        obs = healer.observe(SID)
        self.assertTrue(obs.turn_running)
        self.assertIsNotNone(obs.cache_age)
        self.assertLessEqual(obs.cache_age, 30)
        self.assertGreater(obs.kb_age, 90)
        self.assertGreater(obs.watcher_age, 90)
        self.assertEqual(evaluate(obs), "dead_channel")

    def test_observe_without_anything_is_no_turn(self):
        obs = healer.observe(SID)
        self.assertFalse(obs.turn_running)
        self.assertEqual(evaluate(obs), "no_turn")


class TestTheLadder(unittest.TestCase):
    """check() with everything injected — the action, not the files."""

    def setUp(self):
        self.zt = support.fresh(self)
        healer.reset_bookkeeping()
        set_active_bot_session({"sid": SID, "focus_sid": SID,
                                "title": "zcode_bot_2",
                                "ws": "/Users/lattenero/Projects/bot"})
        self.attached = []
        self.sent = []

    def tearDown(self):
        healer.reset_bookkeeping()

    def _deps(self, obs, verdict="dead_channel"):
        dead = dict(observe=lambda sid, now=None: obs,
                    attach=lambda cfg, sid, title, ws:
                        self.attached.append((sid, title, ws)) or "attached",
                    send=lambda cfg, text, now=None:
                        self.sent.append(text) or text)
        if verdict == "dead_channel":
            return dead
        obs2 = Obs(turn_running=False)
        dead["observe"] = lambda sid, now=None: obs2
        return dead

    def _dead_obs(self):
        return Obs(turn_running=True, cache_age=8, kb_age=200)

    def test_strike1_heals_silently_the_bundle_reports(self):
        # the first heal says NOTHING in-band: F78c's 📥 bundle is the
        # report (it lands what the chat never saw at the first sync)
        out = healer.check({}, deps=self._deps(self._dead_obs()), now=1000.0)
        self.assertIsNone(out)
        self.assertEqual(len(self.attached), 1)
        self.assertEqual(self.attached[0][0], SID)

    def test_strike2_heals_and_reports_with_the_cause(self):
        with open(os.path.join(config.LOG_DIR, "errors.log"), "w") as f:
            f.write("[2026-10-27 14:25:48.1 -03] ERROR zcode_bot.errors "
                    "component=cot-live cause=KeyError: ph\n")
        for t in (1000.0, 1021.0):   # past the throttle
            healer.check({}, deps=self._deps(self._dead_obs()), now=t)
        self.assertEqual(len(self.attached), 2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("💀", self.sent[0])
        self.assertIn("KeyError: ph", self.sent[0])
        self.assertIn("🔁", self.sent[0])

    def test_strike3_advises_nuclear_and_stops_respawning(self):
        for t in (1000.0, 1021.0, 1042.0):
            healer.check({}, deps=self._deps(self._dead_obs()), now=t)
        self.assertEqual(len(self.attached), 2)   # strikes 1 and 2 only
        self.assertEqual(len(self.sent), 2)
        self.assertIn("☢️", self.sent[1])
        self.assertIn("/nuclear", self.sent[1])
        # strike 4 inside the cooldown: NOTHING (no spam, no loop)
        out = healer.check({}, deps=self._deps(self._dead_obs()), now=1063.0)
        self.assertIsNone(out)
        self.assertEqual(len(self.sent), 2)

    def test_healthy_cycle_resets_the_strikes(self):
        healer.check({}, deps=self._deps(self._dead_obs()), now=1000.0)
        healer.check({}, deps=self._deps(None, verdict="ok"), now=1021.0)
        out = healer.check({}, deps=self._deps(self._dead_obs()), now=1042.0)
        # a fresh episode starts at strike 1 again: silent heal
        self.assertIsNone(out)
        self.assertEqual(len(self.attached), 2)
        self.assertEqual(self.sent, [])

    def test_throttled_between_checks(self):
        a = healer.check({}, deps=self._deps(self._dead_obs()), now=1000.0)
        b = healer.check({}, deps=self._deps(self._dead_obs()), now=1001.0)
        self.assertIsNone(a)  # silent first strike
        self.assertIsNone(b)  # throttled: not even observed
        self.assertEqual(len(self.attached), 1)

    def test_no_focus_no_heal(self):
        set_active_bot_session({"sid": "", "focus_sid": "", "ws": ""})
        healer.check({}, deps=self._deps(self._dead_obs()), now=1000.0)
        self.assertEqual(self.attached, [])


if __name__ == "__main__":
    unittest.main()
