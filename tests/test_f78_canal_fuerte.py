"""F78 — the thinkings channel as strong as the conclusions channel.

27/10, live forensics on the diff_ask task: a 163k-char pass sealed ~54
boxes into the ledger, only ~14 traveled — the 3k grid crossed BETWEEN
polls and the box was never born live (dead birth); the muted windows
produced more. Damian's directive: the channel that carries the
model's conclusions ALWAYS delivers — «el canal de los thinkings debe  # noqa: spanish — Damian's directive, quoted
ser igual de correcto». Three guarantees now:  # noqa: spanish — Damian's directive, quoted (continuation)

- F78a: a tool mid-run (a LONG program) shows 🔧 with its OWN clock —
  the log's tool.call.started/completed truth (the store settles tool
  parts only at completion; a 68 s Bash was invisible, the Mac
  blinking «running» all along).
- F78b: a box that crosses the grid without a live message is born
  DONE, with its button, in the same poll — no dead births.
- F78c: whatever the chat never saw (muted window, failed send) lands
  as ONE collapsed message at the re-attach or the close — never
  silent loss.
"""
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import cli_logs  # noqa: E402
from zcode_bot import core as zc_core  # noqa: E402
from zcode_bot import reasoning  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
T0 = int(time.time()) - 90
SID = "sess_f78c0de5-1111-4222-8333-9f78a0dee0f5"
OTHER = "sess_f78_other-1111-4222-8333-9f78a0dee0f5"
CAP = reasoning.COT_BOX_CAP


def _msg(role, *parts):
    return {"info": {"role": role}, "parts": list(parts)}


def _r(text):
    return {"type": "reasoning", "text": text}


def _ev(sid, ev, ts, tool="Bash"):
    d = {"sessionId": sid, "event": ev, "timestamp": "x",
         "context": {"toolName": tool}}
    return (ts, sid, ev, d)


def _fake_ev(events):
    """iter_ev injection: a callable honoring the scanner's contract
    (the sids filter is iter_events' job — the faithful fake carries
    it, so the replay layer stays thin like agent_request_activity's)."""
    def it(base, needles=(), sids=None, **kw):
        return [e for e in events if sids is None or e[1] in sids]
    return it


class TestToolInFlight(unittest.TestCase):
    """F78a — the unit: the log replay that says WHICH tool runs since
    WHEN. started without completed = in flight; completed clears; a
    second started replaces the first; other sessions never mix in."""

    def test_started_without_completed_is_in_flight(self):
        now = 1000.0
        fake = _fake_ev([
            _ev(SID, "tool.call.started", 950.0, tool="Bash"),
            _ev(OTHER, "tool.call.started", 960.0, tool="Edit"),
        ])
        self.assertEqual(cli_logs.tool_in_flight(SID, now=now, iter_ev=fake,
                                                 base="/x"),
                         ("Bash", 950.0))

    def test_completed_clears_and_a_new_started_wins(self):
        now = 1000.0
        fake = _fake_ev([
            _ev(SID, "tool.call.started", 900.0, tool="Bash"),
            _ev(SID, "tool.call.completed", 920.0),
            _ev(SID, "tool.call.started", 940.0, tool="Grep"),
        ])
        self.assertEqual(cli_logs.tool_in_flight(SID, now=now, iter_ev=fake,
                                                 base="/x"),
                         ("Grep", 940.0))
        fake2 = _fake_ev([_ev(SID, "tool.call.started", 900.0, tool="Bash"),
                          _ev(SID, "tool.call.completed", 990.0)])
        self.assertIsNone(cli_logs.tool_in_flight(
            SID, now=now, iter_ev=fake2, base="/x"))

    def test_another_sessions_tool_never_answers(self):
        now = 1000.0
        fake = _fake_ev([_ev(OTHER, "tool.call.started", 950.0)])
        self.assertIsNone(cli_logs.tool_in_flight(
            SID, now=now, iter_ev=fake, base="/x"))

    def test_the_line_carries_the_tools_own_clock(self):
        # the 🔧 state with the log's tool: the verb vocabulary + the
        # tool's OWN elapsed (it ticks every poll — a long program
        # keeps the line alive instead of freezing it)
        act = {"tool": "Bash", "running": True, "steps": 2,
               "tool_since": time.time() - 100.4}
        line = reasoning._live_line_text("t", time.time() - 200, None,
                                         act, 3)
        self.assertIn("Running Bash", line)
        self.assertIn("· 100s", line)
        self.assertIn("🔧", line)

    def test_the_store_tool_still_wins_with_its_detail(self):
        act = {"tool": "Edit", "running": True, "steps": 2,
               "file": "/w/reasoning.py"}
        line = reasoning._live_line_text("t", time.time() - 30, None, act, 0)
        self.assertIn("Editing: reasoning.py", line)
        self.assertNotIn("· ", line.split("\n")[1])  # no clock without since


class TestRunningFromTheLog(unittest.TestCase):
    """F78a — the integration: the watcher's line goes 🔧 from the
    LOG's truth while a long program runs — the store cannot say it."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits = [], []
        support.patch(self, reasoning.uhttp, "send",
                      lambda cfg, text, loud=None, markup=None:
                      (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((text, markup)), True)[1])
        support.patch(self, reasoning.time, "sleep", lambda s: None)
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")

    def test_a_long_bash_shows_running_with_its_clock(self):
        started = time.time() - 41
        support.patch(self, cli_logs, "tool_in_flight",
                      lambda sid, **kw: ("Bash", started))
        support.patch(self, reasoning, "_pass_count", lambda sid, t0: 1)

        class _ProtoSilent:
            """The store settles nothing live (k empty — a bulk Mac
            turn) and the sidecar is gone: only the log knows."""

            def __init__(self, marker):
                self.marker, self.n = marker, 0
                self.bare = [_msg("user", {"type": "text", "text": "p"})]

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 2:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": self.bare}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoSilent(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "f78", T0, "/w")
        lines = [t for t, _m in self.sends] + [t for t, _m in self.edits]
        self.assertTrue(any("Running Bash" in t and "· 41s" in t
                            for t in lines), lines)


class TestBornDone(unittest.TestCase):
    """F78b — no dead births: the grid crossed BETWEEN polls and the
    box was never born live; before, it died in the ledger (27/10: ~54
    sealed, ~14 traveled). Now it is born DONE with its button, the
    same poll."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits = [], []
        support.patch(self, reasoning.uhttp, "send",
                      lambda cfg, text, loud=None, markup=None:
                      (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((text, markup)), True)[1])
        support.patch(self, reasoning.time, "sleep", lambda s: None)
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")

    def test_two_grids_in_one_poll_both_travel(self):
        """The fast pass: the tail jumps past TWO grids between polls —
        the first box captures the live message it had, the second is
        born DONE with its button. Nothing dies in the ledger."""
        from zcode_bot import live_io as lio
        support.patch(self, reasoning, "_pass_count", lambda sid, t0: 1)
        A = "a" * CAP + "b" * CAP + "c" * 400
        stages = [
            {"reasoningChars": 100, "textChars": 0,
             "reasoningTail": "a" * 100, "textTail": ""},
            # the jump: 100 → 2*CAP+400 in ONE flush (the poll slept
            # long, the reasoning ran fast)
            {"reasoningChars": len(A), "textChars": 0,
             "reasoningTail": A, "textTail": ""},
            None,   # the request completed: the tail seals
        ]
        calls = [0]

        def fake_latest(sid, max_age=900.0, read=None):
            i = min(calls[0], len(stages) - 1)
            calls[0] += 1
            return stages[i]
        support.patch(self, lio, "latest", fake_latest)

        class _ProtoBulk:
            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 4:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": [
                        _msg("user", {"type": "text", "text": "p"})]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoBulk(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "f78", T0, "/w")
        # the sealed views: every box solidified with ITS button — the
        # live one via the edit, the dead-born one via its own message
        seals = [(t, m) for t, m in self.edits if m] \
            + [(t, m) for t, m in self.sends if m]
        self.assertTrue(any("<b>CoT 1.1</b>" in t for t, _m in seals),
                        self.edits + self.sends)
        born_done = [(t, m) for t, m in self.sends
                     if m and "<b>CoT 1.2</b>" in t]
        self.assertTrue(born_done, self.sends)
        btns = [b["text"] for r in born_done[0][1]["inline_keyboard"]
                for b in r]
        self.assertEqual(btns, ["CoT 1.2"])
        # and the cache tells the truth: EVERY box has its message id
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 1.2", "CoT 1.3"])
        self.assertTrue(all(d["phs"]), d["phs"])
        self.assertTrue(d["done"])


class TestMissedLandCollapsed(unittest.TestCase):
    """F78c — the muted window: the focus was elsewhere while the pass
    sealed. The boxes live in the ledger without messages; the chat
    gets ONE collapsed landing (count + buttons) — the same delivery
    guarantee the model's conclusions have. Never silent loss."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits = [], []
        support.patch(self, reasoning.uhttp, "send",
                      lambda cfg, text, loud=None, markup=None:
                      (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((text, markup)), True)[1])
        support.patch(self, reasoning.time, "sleep", lambda s: None)
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")
        # the gate ON: the stream is muted until the task is current
        self.gate_cfg = dict(CFG, only_active_task=True)
        zc_core.set_active_bot_session({"sid": OTHER, "title": "other",
                                        "ws": "/o"})

    def test_the_close_lands_the_unseen_boxes_as_one_bundle(self):
        from zcode_bot import live_io as lio
        support.patch(self, reasoning, "_pass_count", lambda sid, t0: 1)
        A = "a" * (CAP + 500)
        stages = [
            {"reasoningChars": 100, "textChars": 0,
             "reasoningTail": "a" * 100, "textTail": ""},
            {"reasoningChars": CAP + 100, "textChars": 0,
             "reasoningTail": A[-CAP:], "textTail": ""},
            None,
        ]
        calls = [0]

        def fake_latest(sid, max_age=900.0, read=None):
            i = min(calls[0], len(stages) - 1)
            calls[0] += 1
            return stages[i]
        support.patch(self, lio, "latest", fake_latest)

        class _ProtoMuted:
            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 4:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": [
                        _msg("user", {"type": "text", "text": "p"})]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoMuted(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(self.gate_cfg, SID, "f78", T0, "/w")
        # muted the whole turn: no cajitas were born... and the close
        # does NOT rescue (the task never became current again) — the
        # cache keeps them for /cot. The mute held; no stray pushes.
        self.assertFalse([t for t, m in self.sends if m], self.sends)
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 1.2"])
        self.assertTrue(d["done"])

    def test_the_repick_lands_them_collapsed_mid_turn(self):
        from zcode_bot import live_io as lio
        support.patch(self, reasoning, "_pass_count", lambda sid, t0: 1)
        A = "a" * (CAP + 500)
        stages = [
            {"reasoningChars": 100, "textChars": 0,
             "reasoningTail": "a" * 100, "textTail": ""},
            {"reasoningChars": CAP + 100, "textChars": 0,
             "reasoningTail": A[-CAP:], "textTail": ""},
            None,
        ]
        calls = [0]

        def fake_latest(sid, max_age=900.0, read=None):
            i = min(calls[0], len(stages) - 1)
            calls[0] += 1
            return stages[i]
        support.patch(self, lio, "latest", fake_latest)

        class _ProtoRepick:
            """The user re-picks the task MID-TURN (poll 3): the muted
            gap's boxes land collapsed at once."""

            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n == 3:
                        zc_core.set_active_bot_session(
                            {"sid": SID, "title": "f78", "ws": "/w"})
                    if self.n > 5:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": [
                        _msg("user", {"type": "text", "text": "p"})]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoRepick(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(self.gate_cfg, SID, "f78", T0, "/w")
        bundles = [(t, m) for t, m in self.sends
                   if m and "cots landed" in t]
        self.assertTrue(bundles, self.sends)
        self.assertIn("2 cots", bundles[0][0])
        btns = [b["text"] for r in bundles[0][1]["inline_keyboard"]
                for b in r]
        self.assertEqual(btns, ["CoT 1.1", "CoT 1.2"])
        # the bundle landed ONCE — the close does not repeat it
        self.assertEqual(len(bundles), 1)
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 1.2"])


if __name__ == "__main__":
    unittest.main()
