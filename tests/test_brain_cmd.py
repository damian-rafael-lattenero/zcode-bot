"""/brain (ADR-019 P2) — retrieval from agent-brain, read-only.

The HTTP seam is brain_client._call: patched here, never urllib itself.
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


def _hit(title, heading, content, score=0.031):
    return {"title": title, "heading": heading, "ordinal": 0,
            "content": content, "score": score, "uri": "/corpus/%s" % title}


def _answer(**hits):
    return {"query": "q", "embedModel": "nomic-embed-text",
            "hits": list(hits.values()), "citations": []}


class TestBrainCommand(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _patch_call(self, responses):
        """responses: path → (status, body). Anything else raises."""
        def fake_call(cfg, path, body=None):
            if path in responses:
                return responses[path]
            raise brain_client.BrainUnavailable("%s not stubbed" % path)
        patcher = mock.patch.object(brain_client, "_call", side_effect=fake_call)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake_call

    def test_usage_without_a_question(self):
        out = zd.handle_command(_cfg(self.zt), "/brain")
        self.assertIn("/brain", out)
        self.assertIn("cited chunks", out)
        self.assertIn("status", out)

    def test_a_question_renders_title_heading_score_and_snippet(self):
        calls = []
        self._patch_call({
            brain_client.PATH_QUERY: (200, _answer(
                tailer=_hit("tailer-truncation.md", "tailer.md > Bus",
                            "tailer.md > Bus\nThe tailer restarts from byte zero when the file shrinks <b>&amp;</b> more")),
            ),
        })
        out = zd.handle_command(_cfg(self.zt), "/brain how does the tailer handle truncation?")
        self.assertIn("how does the tailer handle truncation?", out)
        self.assertIn("tailer-truncation.md", out)
        self.assertIn("tailer.md &gt; Bus", out)
        self.assertIn("restarts from byte zero", out)
        self.assertIn("nomic-embed-text", out)
        self.assertNotIn("<b>&", out, "the snippet must be escaped")

    def test_no_hits_says_so(self):
        self._patch_call({brain_client.PATH_QUERY: (200, _answer())})
        out = zd.handle_command(_cfg(self.zt), "/brain nothing matches this")
        self.assertIn("nothing indexed matches", out)

    def test_unreachable_brain_points_at_serve(self):
        def dead(cfg, path, body=None):
            raise brain_client.BrainUnavailable("http://127.0.0.1:1/rag/query (refused)")
        patcher = mock.patch.object(brain_client, "_call", side_effect=dead)
        patcher.start()
        self.addCleanup(patcher.stop)
        out = zd.handle_command(_cfg(self.zt), "/brain anything")
        self.assertIn("brain unreachable", out)
        self.assertIn("agent-brain serve", out)

    def test_status_renders_the_three_counters(self):
        self._patch_call({
            brain_client.PATH_READY: (200, {"status": "ok", "version": "0.2.0",
                                            "checks": {"db": {"ok": True},
                                                       "embed": {"ok": True, "model": "nomic-embed-text"}}}),
            brain_client.PATH_STATS: (200, {"documents": 13, "chunks": 21, "bySource": {}}),
            brain_client.PATH_BUS: (200, {"dbEvents": 7, "lagBytes": 0, "duplicates": 3, "malformed": 1}),
        })
        out = zd.handle_command(_cfg(self.zt), "/brain status")
        for needle in ("0.2.0", "nomic-embed-text", "13", "21", "7", "3", "1"):
            self.assertIn(needle, out, out)

    def test_a_4xx_comes_back_as_the_error_body(self):
        self._patch_call({brain_client.PATH_QUERY: (400, {"error": "k must be between 1 and 50"})})
        out = zd.handle_command(_cfg(self.zt), "/brain with a broken k")
        self.assertIn("brain unreachable", out)
        self.assertIn("k must be between", out)


if __name__ == "__main__":
    unittest.main()
