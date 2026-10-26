"""F65c — the /task list: BOTH marks on the active task that runs (the
WIP emoji used to swallow the dart), and each task's subagents nested
under it, tappable with the same sa: pick as /subagents."""
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import agent_live, reporting  # noqa: E402


class TestTaskList(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _tasks(self, sids):
        return [{"sid": s, "n": i + 1, "ws": "/w", "title": "task %d" % (i + 1)}
                for i, s in enumerate(sids)]

    def _list(self, sids, running=(), active=None):
        support.patch(self, reporting, "_tasks_scope",
                      lambda scope, n: self._tasks(sids))
        support.patch(self, reporting, "running_turns", lambda: set(running))
        support.patch(self, reporting, "active_bot_session",
                      lambda: {"sid": active} if active else {})
        return reporting.task_list_report("projects")

    def test_active_running_task_shows_both_marks(self):
        text, kb = self._list(["ses_a", "ses_b"], running={"ses_a"},
                              active="ses_a")
        self.assertIn("🔄🎯 1. task 1", text)
        self.assertIn("· 2. task 2", text)      # neither running nor active
        self.assertIn("🔄🎯 1", kb["inline_keyboard"][0][0]["text"])

    def test_active_but_idle_shows_only_the_dart(self):
        text, _ = self._list(["ses_a"], running=set(), active="ses_a")
        self.assertIn("🎯 1. task 1", text)
        self.assertNotIn("🔄", text)

    def test_agents_nested_under_their_task(self):
        with open(agent_live.index_path("ses_a"), "w") as f:
            json.dump({"sid": "ses_a", "t0": 500, "title": "task 1",
                       "updated": time.time(),
                       "agents": [{"n": 1, "label": "Probador A",
                                   "state": "running", "req_n": 2,
                                   "boxes": 1, "prompt": "p", "t0": 500}]}, f)
        text, kb = self._list(["ses_a"])
        self.assertIn("Probador A", text)
        self.assertLess(text.index("task 1"), text.index("Probador A"))
        data = [b["callback_data"]
                for row in kb["inline_keyboard"] for b in row]
        self.assertIn("t:ses_a", data)              # the task itself…
        self.assertIn("sa:ses_a:500:1", data)       # …then its agent

    def test_stale_agent_index_is_not_nested(self):
        with open(agent_live.index_path("ses_a"), "w") as f:
            json.dump({"sid": "ses_a", "t0": 500, "title": "task 1",
                       "updated": time.time() - 4 * agent_live.OTHERS_FRESH_S,
                       "agents": [{"n": 1, "label": "Old", "state": "done",
                                   "req_n": 1, "prompt": "p"}]}, f)
        text, _ = self._list(["ses_a"])
        self.assertNotIn("Old", text)


if __name__ == "__main__":
    unittest.main()
