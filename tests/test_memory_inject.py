"""ADR-020 — memory injection on a REAL phone turn (prompt_worker on the fake runtime).

The block rides in front of the prompt the runtime receives; the bus keeps the
ORIGINAL text plus the memory ids (provenance, and no extraction feedback loop);
a dead brain costs nothing but the plain prompt.
"""
import json
import os
from unittest import mock

import support
from test_e2e_turno import BATCHES_2PASSES, _E2EBase

from zcode_bot import brain_client  # noqa: E402
from zcode_bot import config as zc_config  # noqa: E402
from zcode_bot.live_view import HEADLESS_GUARD  # noqa: E402


def _bus_lines():
    if not os.path.exists(zc_config.BUS_FILE):
        return []
    with open(zc_config.BUS_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines() if line]


class TestMemoryInjectTurn(_E2EBase):
    def _sent_content(self, runtime):
        sends = [p for m, p in runtime.requests if m == "session/send"]
        self.assertEqual(len(sends), 1, runtime.requests)
        return sends[0]["content"]

    def test_the_block_leads_the_prompt_and_the_bus_keeps_the_original(self):
        def fake_call(cfg, path, body=None):
            self.assertEqual(path, brain_client.PATH_MEMORY_SEARCH)
            self.assertEqual(body["query"], "do something with several tools")
            return 200, {"query": body["query"], "embedModel": "m", "memories": [
                {"id": 12, "kind": "preference", "content": "Prefers Amazon Corretto JDKs via brew casks."},
                {"id": 40, "kind": "decision", "content": "The core is Kotlin, not Python."},
            ]}
        p = mock.patch.object(brain_client, "_call", side_effect=fake_call)
        p.start()
        self.addCleanup(p.stop)
        runtime = self._run_turn(BATCHES_2PASSES, event_bus=True, memory_inject=True, cot_buttons=False)
        content = self._sent_content(runtime)
        self.assertTrue(content.startswith("[memory · agent-brain · 2 item(s) · ids 12,40]\n"), content[:120])
        self.assertIn("- preference #12: Prefers Amazon Corretto JDKs via brew casks.", content)
        self.assertIn("- decision #40: The core is Kotlin, not Python.", content)
        self.assertIn("—\ndo something with several tools", content)
        self.assertTrue(content.endswith(HEADLESS_GUARD), "the guardrail still closes the prompt")
        started = [e for e in _bus_lines() if e["kind"] == "turn.started"][0]
        self.assertEqual(started["payload"]["prompt"], "do something with several tools")
        self.assertEqual(started["payload"]["memory_ids"], [12, 40])
        self.assertIn("memory_inject: 2 item(s) [12, 40]", self._pwlog())

    def test_a_dead_brain_leaves_the_prompt_untouched(self):
        def dead(cfg, path, body=None):
            raise brain_client.BrainUnavailable("refused")
        p = mock.patch.object(brain_client, "_call", side_effect=dead)
        p.start()
        self.addCleanup(p.stop)
        runtime = self._run_turn(BATCHES_2PASSES, event_bus=True, memory_inject=True, cot_buttons=False)
        content = self._sent_content(runtime)
        self.assertTrue(content.startswith("do something with several tools"))
        started = [e for e in _bus_lines() if e["kind"] == "turn.started"][0]
        self.assertEqual(started["payload"]["memory_ids"], [])

    def test_without_the_knob_the_brain_is_never_asked(self):
        calls = []
        p = mock.patch.object(brain_client, "_call", side_effect=lambda *a, **k: calls.append(a) or (200, {}))
        p.start()
        self.addCleanup(p.stop)
        runtime = self._run_turn(BATCHES_2PASSES, cot_buttons=False)
        self.assertTrue(self._sent_content(runtime).startswith("do something with several tools"))
        self.assertEqual(calls, [])
