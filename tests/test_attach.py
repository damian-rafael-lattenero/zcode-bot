"""F62 — /task on a mid-turn session attaches to the turn.

The rollout replay (turn_snapshot), the recap (catchup_text), the
spectator single-flight pid-file and the mac_attach orchestration with
injected deps (nothing is spawned, nothing is sent for real).
"""
import datetime
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import attach  # noqa: E402
from zcode_bot import config  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
from zcode_bot import spectator  # noqa: E402

SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
T0 = 1760000000


def _iso(ms):
    return datetime.datetime.fromtimestamp(
        ms / 1000.0, tz=datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z")


def _rec(turn, ms, text, tools=()):
    return {"querySource": "main_turn", "turnId": turn,
            "startedAt": _iso(ms),
            "response": {"finishReason": "tool-calls", "text": text,
                         "toolCalls": [{"name": n, "input": i}
                                       for n, i in tools]}}


class TestSnapshotAndCatchup(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _write_rollout(self, records):
        p = config.rollout_path(SID)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_snapshot_pins_the_turn_and_collects(self):
        old_ms = (T0 - 300) * 1000.0
        now_ms = time.time() * 1000.0 - 500.0
        self._write_rollout([
            _rec("old-turn", old_ms, "old narration",
                 [("Read", {"path": "x.py"})]),
            _rec("t1", now_ms, "current narration",
                 [("Bash", {"command": "ls"}),
                  ("Edit", {"path": "y.py"})]),
        ])
        steps, sent = attach.turn_snapshot(SID, now_ms - 30000.0)
        self.assertEqual(steps, 2)          # the old turn does not exist
        self.assertTrue(any("current narration" in t for t in sent))
        self.assertFalse(any("old narration" in t for t in sent))

    def test_snapshot_without_rollout(self):
        steps, sent = attach.turn_snapshot(SID, time.time() * 1000.0)
        self.assertEqual((steps, sent), (0, []))

    def test_catchup_drops_the_spectator_label_and_caps_the_tail(self):
        sent = ["🖥️ <b>«t»</b> — Mac turn live (spectator)"] + \
               ["box %d" % i for i in range(8)] + \
               ["🖥️ <b>«t»</b>\nsome narration"]
        text = attach.catchup_text("t", 8, sent)
        self.assertIn("(+3 earlier steps)", text)
        self.assertNotIn("Mac turn live (spectator)", text)
        self.assertIn("some narration", text)
        self.assertNotIn("box 0", text)      # the oldest fell off the tail

    def test_catchup_nothing_happened_is_none(self):
        self.assertIsNone(attach.catchup_text("t", 0, []))


class TestSpectatorSingleFlight(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_alive_false_without_file(self):
        self.assertFalse(spectator.alive(SID, proc_alive=lambda pid: True))

    def test_pid_file_older_than_the_marker_is_stale(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(spectator.spec_pid_path(SID), "w") as f:
            f.write("4242")
        old = time.time() - 600
        os.utime(spectator.spec_pid_path(SID), (old, old))
        with open(config.marker_path(SID), "w") as f:
            json.dump({"token": "t-new"}, f)
        # 25/10: the previous turn's spectator, pinned to its turnId,
        # must not block THIS turn's spawn
        self.assertFalse(spectator.alive(SID, proc_alive=lambda pid: True))

    def test_alive_true_with_live_pid_and_garbage_is_false(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(spectator.spec_pid_path(SID), "w") as f:
            f.write("4242")
        self.assertTrue(spectator.alive(SID, proc_alive=lambda pid: pid == 4242))
        self.assertFalse(spectator.alive("sess_otra", proc_alive=lambda pid: True))
        with open(spectator.spec_pid_path(SID), "w") as f:
            f.write("not a pid")
        self.assertFalse(spectator.alive(SID, proc_alive=lambda pid: True))


class TestWorkerSummary(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_counts_the_boxes_of_the_live_cache(self):
        reasoning.save_cache(SID, T0, "title", ["a", "b"], ["CoT 1.1", "CoT 1.2"],
                             live=True)
        line = attach.worker_summary(SID, T0, now=lambda: T0 + 120)
        self.assertIn("2 CoT boxes", line)
        self.assertIn("2 min", line)

    def test_without_cache_still_reports_zero_boxes(self):
        line = attach.worker_summary(SID, T0, now=lambda: T0 + 60)
        self.assertIn("0 CoT boxes", line)


class TestMacAttach(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        os.makedirs(config.STATE_DIR, exist_ok=True)
        # an in-flight Mac turn: marker + rollout with two steps
        with open(config.marker_path(SID), "w") as f:
            json.dump({"token": "tok1", "prompt": "p"}, f)
        p = config.rollout_path(SID)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(json.dumps(_rec(
                "t1", time.time() * 1000.0 - 1000.0, "narr",
                [("Bash", {"command": "ls"}), ("Read", {"path": "x"})])) + "\n")
        self.spawned, self.sends = [], []
        self.deps = {"send": lambda cfg, text, loud=None, markup=None:
                     (self.sends.append(text), True)[1],
                     "spawn": self.spawned.append,
                     "alive": lambda sid: False,
                     "watcher_alive": lambda sid: False,
                     "agent_alive": lambda sid: False}

    def test_dead_watchers_respawn_and_the_recap_arrives(self):
        out = attach.mac_attach({"token": "T", "chat_id": 1}, SID,
                                "title", "/ws", deps=self.deps)
        self.assertEqual(self.spawned[0][:2], ["--cot-live", SID])
        self.assertEqual(self.spawned[1][:2], ["--spectator", SID])
        self.assertEqual(self.spawned[2][:2], ["--agent-live", SID])  # F63
        self.assertTrue(self.sends, "the recap is a message of its own")
        self.assertIn("attaching mid-turn", self.sends[0])
        self.assertIn("2 steps so far", out)
        self.assertIn("respawned", out)

    def test_live_watchers_spawn_nothing_and_recap_nothing(self):
        self.deps["alive"] = lambda sid: True
        self.deps["watcher_alive"] = lambda sid: True
        self.deps["agent_alive"] = lambda sid: True
        out = attach.mac_attach({"token": "T", "chat_id": 1}, SID,
                                "title", "/ws", deps=self.deps)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.sends, [])
        self.assertIn("already streaming", out)
        self.assertIn("already live", out)
        self.assertIn("watching", out)

    def test_no_marker_is_the_honest_generic_line(self):
        os.remove(config.marker_path(SID))
        out = attach.mac_attach({"token": "T", "chat_id": 1}, SID,
                                "title", "/ws", deps=self.deps)
        self.assertIn("A turn is running", out)
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()
