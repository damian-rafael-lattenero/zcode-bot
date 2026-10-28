"""ADR-020 — /memory (read side) and the injection knob's command surface.

The HTTP seam is brain_client._call: patched here, never urllib.
"""
import unittest
from unittest import mock

import support
from zcode_bot import brain_client
from zcode_bot.commands import dispatch as zd


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, enabled=True)
    cfg.update(extra)
    return cfg


def _mem(mid, kind, content):
    return {"id": mid, "kind": kind, "content": content, "createdAt": "2026-10-28T21:00:00Z",
            "sessionId": "s", "turnId": "1", "salience": 1.0, "score": 0.03}


class TestMemoryCommand(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _patch(self, responses):
        def fake_call(cfg, path, body=None):
            key = path.split("?")[0]
            if key in responses:
                return responses[key]
            raise brain_client.BrainUnavailable("%s not stubbed" % path)
        p = mock.patch.object(brain_client, "_call", side_effect=fake_call)
        p.start()
        self.addCleanup(p.stop)

    def test_usage(self):
        out = zd.handle_command(_cfg(self.zt), "/memory")
        self.assertIn("/memory recent", out)
        self.assertIn("/memory status", out)

    def test_query_renders_kind_id_date_and_content_escaped(self):
        self._patch({
            brain_client.PATH_MEMORY_SEARCH: (200, {
                "query": "which jdk", "embedModel": "nomic-embed-text",
                "memories": [_mem(12, "preference", "Prefers Amazon Corretto JDKs <b>via</b> brew casks.")]}),
        })
        out = zd.handle_command(_cfg(self.zt), "/memory which jdk do I use")
        self.assertIn("1 memory", out)
        self.assertIn("preference</i> #12", out)
        self.assertIn("2026-10-28", out)
        self.assertIn("Amazon Corretto JDKs", out)
        self.assertIn("&lt;b&gt;via&lt;/b&gt;", out)

    def test_recent_and_status_and_empty(self):
        self._patch({
            brain_client.PATH_MEMORY_RECENT: (200, [_mem(3, "fact", "The core is Kotlin.")]),
            brain_client.PATH_MEMORY_STATUS: (200, {"memories": 9, "byKind": {"preference": 4, "fact": 5},
                                                    "pendingTurns": 1, "processedTurns": 7, "quarantined": 0,
                                                    "chatModel": "qwen2.5:1.5b", "breaker": "Closed"}),
            brain_client.PATH_MEMORY_SEARCH: (200, {"query": "q", "embedModel": "m", "memories": []}),
        })
        recent = zd.handle_command(_cfg(self.zt), "/memory recent")
        self.assertIn("The core is Kotlin.", recent)
        status = zd.handle_command(_cfg(self.zt), "/memory status")
        for needle in ("9 memories", "4 preference", "5 fact", "pending 1", "qwen2.5:1.5b", "Closed"):
            self.assertIn(needle, status)
        empty = zd.handle_command(_cfg(self.zt), "/memory nothing about this")
        self.assertIn("no memories match", empty)

    def test_unreachable_brain_points_at_serve(self):
        def dead(cfg, path, body=None):
            raise brain_client.BrainUnavailable("http://x/memory/search (refused)")
        p = mock.patch.object(brain_client, "_call", side_effect=dead)
        p.start()
        self.addCleanup(p.stop)
        out = zd.handle_command(_cfg(self.zt), "/memory anything")
        self.assertIn("brain unreachable", out)
        self.assertIn("agent-brain serve", out)

    def test_the_context_block_is_bounded_cited_and_best_effort(self):
        cfg = _cfg(self.zt, memory_inject=True, memory_inject_k=3, memory_inject_max_chars=120)
        self._patch({
            brain_client.PATH_MEMORY_SEARCH: (200, {
                "query": "q", "embedModel": "m",
                "memories": [_mem(1, "fact", "short fact one"),
                             _mem(2, "fact", "a fact that is long " * 10),
                             _mem(3, "fact", "another one")]}),
        })
        block, ids = brain_client.memory_context(cfg, "my question")
        self.assertEqual(ids, [1])  # the second blows the cap; the third never fits
        self.assertTrue(block.startswith("[memory · agent-brain · 1 item(s) · ids 1]"))
        self.assertIn("- fact #1: short fact one", block)
        self.assertTrue(block.endswith("—\n"))

        self.assertEqual(brain_client.memory_context(dict(cfg, memory_inject=False), "q"), ("", []))
        self.assertEqual(brain_client.memory_context(cfg, "   "), ("", []))

        def down(cfg, path, body=None):
            raise brain_client.BrainUnavailable("down")
        p2 = mock.patch.object(brain_client, "_call", side_effect=down)
        p2.start()
        self.addCleanup(p2.stop)
        self.assertEqual(brain_client.memory_context(cfg, "q"), ("", []))


if __name__ == "__main__":
    unittest.main()
