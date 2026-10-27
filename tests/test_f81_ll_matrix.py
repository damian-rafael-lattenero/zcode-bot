"""F81 — the thinking line's STATE MATRIX with an unavoidable default.

Damian's directive, 27/10: test EVERY way ZCode can be "working" —
terminal commands, long edits, test runs, every spinner moment — and
whatever cannot be classified must FALL BACK to the thinking default:
«y si no, que caiga en un default de thinking». The classifier is pure
(_ll_state), the line renders from it, the default carries REAL data
(the fork's reasoning chars) and every state TRANSITION is kb-logged —
the blind spot that made the spinner report unauditable."""
import time
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot import reasoning
from zcode_bot.reasoning import _fmt_chars, _ll_state, _live_line_text


def _act(tool="", running=False, since=None):
    return {"pending": 0, "tool": tool, "running": running, "steps": 1,
            **({"tool_since": since} if since else {})}


class TestTheClassifier(unittest.TestCase):
    """_ll_state — every working mode maps to exactly one kind."""

    def test_pure_spinner_is_thinking(self):
        # no sidecar, no tool: the Mac blinks its spinner — the default
        self.assertEqual(_ll_state(None, _act()), "thinking")

    def test_reasoning_stream_is_still_thinking(self):
        # the sidecar flows reasoning chars: still the spinner state
        lv = {"reasoningChars": 1500, "textChars": 0}
        self.assertEqual(_ll_state(lv, _act()), "thinking")

    def test_writing_only_when_text_flows(self):
        lv = {"reasoningChars": 0, "textChars": 400}
        self.assertEqual(_ll_state(lv, _act()), "writing")

    def test_terminal_command_is_the_tool(self):
        self.assertEqual(_ll_state(None, _act("Bash", True)), "tool:Bash")

    def test_long_edit_is_the_tool(self):
        self.assertEqual(_ll_state(None, _act("Edit", True)), "tool:Edit")

    def test_test_runs_are_bash_like_any_program(self):
        # a pytest run is a Bash call — no special case needed
        self.assertEqual(_ll_state(None, _act("Bash", True)), "tool:Bash")

    def test_read_search_write_all_classify(self):
        for name in ("Read", "Grep", "Glob", "Write", "Agent", "Skill",
                     "WebFetch", "TodoWrite"):
            self.assertEqual(_ll_state(None, _act(name, True)),
                             "tool:%s" % name)

    def test_unknown_tool_classifies_as_itself_and_still_renders(self):
        # a tool the vocabulary never met: still a tool line (the verb
        # falls back to "Running <name>")
        kind = _ll_state(None, _act("SomeNewTool", True))
        self.assertEqual(kind, "tool:SomeNewTool")
        line = _live_line_text("t", time.time() - 5, None,
                               _act("SomeNewTool", True), 0)
        self.assertIn("Running SomeNewTool", line)

    def test_a_settled_tail_tool_is_NOT_running(self):
        # the store settles tool parts at completion: the tail tool of
        # the LAST message sits there with running=False — that moment
        # is thinking (the model works BETWEEN tools), never a stale 🔧
        self.assertEqual(_ll_state(None, _act("Bash", False)), "thinking")


class TestTheDefaultCarriesData(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_pure_spinner_without_sidecar_renders_the_plain_default(self):
        line = _live_line_text("t", time.time() - 7, None, _act(), 0)
        self.assertTrue(line.startswith("🧠"))
        self.assertIn("thinking…", line)
        self.assertNotIn("chars", line)

    def test_spinner_with_reasoning_chars_shows_them(self):
        lv = {"reasoningChars": 1530, "textChars": 0}
        line = _live_line_text("t", time.time() - 7, lv, _act(), 1)
        self.assertTrue(line.startswith("💭"))   # the blink's other face
        self.assertIn("thinking… · 1.5k chars", line)

    def test_fmt_chars(self):
        self.assertEqual(_fmt_chars(950), "950")
        self.assertEqual(_fmt_chars(1530), "1.5k")
        self.assertEqual(_fmt_chars(12000), "12.0k")


class TestTransitionTrace(unittest.TestCase):
    """The watcher kb-logs ONE line per state change — the audit trail
    the spinner report lacked (which state did the line show?)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_transitions_land_in_the_kb_log(self):
        import os
        # the scripted f41-style flow is heavy; here drive the WATCHER
        # with the lightest possible proto: no events at all — the ll
        # must still be born, reach 'thinking', and the transition kb
        # must exist in prompt-worker.log (state=thinking after born)
        CFG = dict(self.zt.DEFAULTS, token="T", chat_id=1, enabled=True,
                   loud_all=False, cot_stream=True)
        SID = "session_TESTF81"
        # _ok() demands the session be the CURRENT task (F71/F15)
        from zcode_bot.core import set_active_bot_session
        set_active_bot_session({"sid": SID, "focus_sid": SID, "ws": "/w",
                                "title": "t"})
        T0 = time.time() - 1
        sent = []

        class _P:
            def __init__(self):
                self.n = 0

            def request(self, method, payload=None, timeout=None):
                self.n += 1
                if method == "session/resume":
                    return {"result": {}}
                return {"result": {"messages": []}}

            def close(self):
                pass

        import support as _sup
        _sup.patch(self, reasoning.zc_proto, "make", lambda **kw: _P())
        _sup.patch(self, reasoning.uhttp, "send",
                   lambda cfg, text, loud=None, markup=None:
                   (sent.append(text), 111)[1])
        _sup.patch(self, reasoning.uhttp, "_live_edit",
                   lambda cfg, mid, text, markup=None: True)
        # the marker: turn running, cut at once after one poll
        _sup.patch(self, reasoning, "_marker_state",
                   lambda sid: (T0, "tok"))
        _sup.patch(self, reasoning, "_yielded", lambda sid: False)
        real_sleep = reasoning.time.sleep
        _sup.patch(self, reasoning.time, "sleep",
                   lambda s: real_sleep(0.05))

        def _cut_after_first_poll():
            # _watch_turn loops while the marker stands; make the SECOND
            # marker read None so the loop cuts after one full pass
            _P.states = 0

            def marker(sid):
                _P.states += 1
                return (T0, "tok") if _P.states <= 3 else None
            _sup.patch(self, reasoning, "_marker_state", marker)
        _cut_after_first_poll()
        try:
            reasoning.run_watcher(CFG, SID, "t", T0, "/w")
        except Exception:
            raise
        pw = os.path.join(self.zt.config.BASE, "hooks", "logs",
                          "prompt-worker.log")
        with open(pw) as f:
            kb = f.read()
        self.assertIn("mac ll born", kb)
        self.assertIn("mac ll state=thinking", kb)


if __name__ == "__main__":
    unittest.main()
