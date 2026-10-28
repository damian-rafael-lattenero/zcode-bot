# -*- coding: utf-8 -*-
"""F29 — adaptive session timeouts (incident 18:10 on 15/10): session
requests (resume/setModel/setMode) return the COMPLETE history (~30s
per request in a large session under load) and with a FIXED 30/60s
timeout they expired → the turn died silently and IN A LOOP (mode_set
only registers after a successful _proto_ready, so the next turn paid
everything again; the user's 4 /compact attempts failed on the same
setModel). Now the ceiling scales with the transcript (60s + 4s/MB,
cap 300) and session/compact uses Damian's rule calibrated with real
compacts (135k tokens → 103s ≈ 0.76 s/ktok): 1.2 s/1k tokens, base 300,
cap 600.

F31 (16/10) — the context figure is read from the rollout with a
growing window (one record weighs as much as the context: with fixed
64KB the parse failed on large sessions) and /ctx shows that number
instead of the session/resume probe (contextUsed=0 always), notifying
when a session was compacted with no subsequent turn."""
import datetime
import json
import os
import unittest
from unittest import mock

import support

from zcode_bot import config as zc_config
from zcode_bot import core as zc_core
from zcode_bot import reporting as zc_reporting


def _jsonl_of(sid, entries):
    """Fake model-io jsonl: each entry is a dict with response."""
    d = os.path.join(zc_config.BASE, "cli", "rollout")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "model-io-%s.jsonl" % sid)
    with open(p, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return p


class TestTimeoutByMb(unittest.TestCase):
    """60s base + 4s/MB, cap 300 — a ceiling, not a wait."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_scales_and_caps(self):
        cases = {0: 60, 5: 80, 25: 160, 60: 300, 100: 300}
        for mb, expected in cases.items():
            with self.subTest(mb=mb), \
                    mock.patch.object(zc_core, "transcript_mb",
                                      return_value=float(mb)):
                self.assertEqual(zc_core._timeout_by_mb("s"), expected)

    def test_new_session_without_file(self):
        # no model-io jsonl → base 60 (the usual default for /task new)
        self.assertEqual(zc_core._timeout_by_mb("sess_INEXISTENTE"), 60)


class TestContextKtokens(unittest.TestCase):
    """REAL context tokens: the last usage in the jsonl."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sid = support.T2

    def test_reads_the_last_canonical_usage(self):
        _jsonl_of(self.sid, [
            {"response": {"usage": {"inputTokens": 90000}}},
            {"response": {"usage": {"inputTokens": 146706,
                                    "cacheReadTokens": 146048}}},
        ])
        self.assertAlmostEqual(zc_core._context_ktokens(self.sid), 146.706)

    def test_fallback_provider_snake_case(self):
        _jsonl_of(self.sid, [
            {"response": {"providerMetadata": {"anthropic": {"usage": {
                "input_tokens": 650,
                "cache_read_input_tokens": 146048}}}}},
        ])
        self.assertAlmostEqual(zc_core._context_ktokens(self.sid), 146.698)

    def test_without_usage_or_file_gives_none(self):
        _jsonl_of(self.sid, [{"response": {"body": "no usage"}}])
        self.assertIsNone(zc_core._context_ktokens(self.sid))
        self.assertIsNone(zc_core._context_ktokens("sess_INEXISTENTE"))
        # broken jsonl: never raises
        p = _jsonl_of(self.sid, [])
        with open(p, "a") as f:
            f.write("{trucha\n")
        self.assertIsNone(zc_core._context_ktokens(self.sid))

    def test_record_larger_than_the_64kb_queue(self):
        # each model-io record weighs as much as the whole context: at
        # 200KB+ the fixed 64KB queue only saw a truncated JSON → None
        # (16/10: a 180k-token session, _compact_timeout fell to base)
        padding = "x" * (300 * 1024)
        _jsonl_of(self.sid, [
            {"request": {"messages": padding},
             "response": {"usage": {"inputTokens": 5000}}},
            {"request": {"messages": padding},
             "response": {"usage": {"inputTokens": 180626}}},
        ])
        self.assertAlmostEqual(zc_core._context_ktokens(self.sid), 180.626)

    def test_last_record_without_usage_uses_the_previous(self):
        _jsonl_of(self.sid, [
            {"response": {"usage": {"inputTokens": 7000}}},
            {"response": {"error": "nothing"}},
        ])
        self.assertAlmostEqual(zc_core._context_ktokens(self.sid), 7.0)


class TestLastUsed(unittest.TestCase):
    """(ktokens, epoch, source) of the last request with usage."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sid = support.T2

    def test_complete_tuple(self):
        _jsonl_of(self.sid, [
            {"completedAt": "2026-10-16T02:03:33.209Z",
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 180626}}},
            {"completedAt": "2026-10-16T14:28:23.507Z",
             "querySource": "main_turn",
             "response": {"usage": {"inputTokens": 45625,
                                    "cacheReadTokens": 2176}}},
        ])
        ktok, epoch, source = zc_core.last_used(self.sid)
        self.assertAlmostEqual(ktok, 45.625)
        expected = datetime.datetime(
            2026, 10, 16, 14, 28, 23, 507000,
            tzinfo=datetime.timezone.utc).timestamp()
        self.assertAlmostEqual(epoch, expected)
        self.assertEqual(source, "main_turn")

    def test_without_completed_at_or_source(self):
        _jsonl_of(self.sid, [{"response": {"usage": {"inputTokens": 900}}}])
        self.assertEqual(zc_core.last_used(self.sid), (0.9, 0.0, ""))

    def test_broken_completed_at_does_not_raise(self):
        _jsonl_of(self.sid, [{"completedAt": "ayer", "querySource": "compact",
                              "response": {"usage": {"inputTokens": 900}}}])
        self.assertEqual(zc_core.last_used(self.sid), (0.9, 0.0, "compact"))

    def test_without_file(self):
        self.assertIsNone(zc_core.last_used("sess_INEXISTENTE"))


class TestContextReport(unittest.TestCase):
    """/ctx (F31): real number from the rollout; honest post-compact notice."""

    COMPACT = 1792115800.0  # epoch of task 2's compact.completed

    def setUp(self):
        self.zt = support.fresh(self)
        tasks = [
            {"n": 1, "sid": support.T1, "ws": "/ws/bot", "ctx": 185000,
             "when": int((self.COMPACT + 3600) * 1000), "model": "GLM-5.3",
             "title": "testing"},
            {"n": 2, "sid": support.T2, "ws": "/ws/cv", "ctx": 50473,
             "when": int((self.COMPACT - 120) * 1000), "model": "GLM-5.3",
             "title": "CV"},
            {"n": 3, "sid": support.T3, "ws": "/ws/bot", "ctx": 68719,
             "when": int((self.COMPACT - 9000) * 1000), "model": "GLM-5.3",
             "title": "MCP"},
        ]
        patches = [
            mock.patch.object(zc_reporting, "_ctx_tasks", return_value=tasks),
            mock.patch.object(zc_reporting, "_compactions",
                              return_value={support.T1: self.COMPACT - 7200,
                                            support.T2: self.COMPACT}),
            mock.patch.object(zc_reporting, "active_workspace_path",
                              return_value="/ws/bot"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # T1: compacted 2h ago and WITH a later request → real number
        _jsonl_of(support.T1, [
            {"completedAt": support._iso(self.COMPACT - 7300),
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 185000}}},
            {"completedAt": support._iso(self.COMPACT + 3600),
             "querySource": "main_turn",
             "response": {"usage": {"inputTokens": 23227}}},
        ])
        # T2: the latest on disk is the compact itself → no later turn
        _jsonl_of(support.T2, [
            {"completedAt": support._iso(self.COMPACT - 1),
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 41112}}},
        ])
        # T3: no rollout or compact → falls back to the tasks-repo number

    def test_real_number_after_the_compact(self):
        lines = zc_reporting.context_report().splitlines()
        self.assertIn("1. 🟢 23.2K (2%) — testing", lines)
        self.assertFalse(any("185" in l for l in lines),
                         "the pre-compact number must not appear")

    def test_compacted_without_later_turn_notifies(self):
        hc = datetime.datetime.fromtimestamp(self.COMPACT).strftime("%H:%M")
        lines = zc_reporting.context_report().splitlines()
        self.assertIn("2. 🧹 compacted %s — the real size shows on the"
                      " next turn (before 50.5K) · 📁cv — CV" % hc, lines)

    def test_without_rollout_falls_to_the_repo(self):
        lines = zc_reporting.context_report().splitlines()
        self.assertIn("3. 🟢 68.7K (7%) — MCP", lines)


class TestCompactTimeout(unittest.TestCase):
    """Damian's rule: 1.2 s/1k tokens; base 300, cap 600."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_linear_rule(self):
        cases = {None: 300,   # no data → the usual default
                 0: 300,      # (base dominates)
                 135: 300,    # 162 by rule, but the 300 base dominates
                 300: 360,    # Damian's exact number (300k → 6 min)
                 600: 600,    # cap
                 900: 600}
        for ktok, expected in cases.items():
            with self.subTest(ktok=ktok), \
                    mock.patch.object(zc_core, "_context_ktokens",
                                      return_value=ktok):
                self.assertEqual(zc_core._compact_timeout("s"), expected)


class _FakeCTimeouts:
    """Fake ProtoClient that RECORDS the timeout of each request."""

    def __init__(self):
        self.seen = []  # (method, timeout)
        self.closed = False

    def request(self, method, params=None, timeout=None):
        self.seen.append((method, timeout))
        return {"result": {}}

    def close(self):
        self.closed = True


class TestProtoReadyTimeouts(unittest.TestCase):
    """_proto_ready uses the SAME per-MB ceiling for resume/setModel/
    setMode — all three return the complete history (incident 18:10)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.c = _FakeCTimeouts()

    def test_all_three_requests_with_the_mb_ceiling(self):
        with mock.patch.object(zc_core, "transcript_mb", return_value=25.0):
            err = zc_core._proto_ready(self.c, support.T2,
                                       skip_model=False, skip_mode=False)
        self.assertIsNone(err)
        expected = 60 + int(25.0 * 4)  # 160
        self.assertEqual(self.c.seen, [
            ("session/resume", expected),
            ("session/setModel", expected),
            ("session/setMode", expected)])

    def test_request_error_propagates_as_message(self):
        c = _FakeCTimeouts()
        c.request = lambda method, params=None, timeout=None: (
            {"error": {"code": -2, "message": "timeout session/resume"}}
            if method == "session/resume" else {"result": {}})
        err = zc_core._proto_ready(c, support.T2)
        self.assertIn("❌ resume:", err)
        self.assertIn("timeout", err)


class TestCompactTimeouts(unittest.TestCase):
    """/compact: resume+setModel with the per-MB ceiling and compact with
    the per-token ceiling — the user's entire flow (4 failed /compact
    attempts today)."""

    def test_timeouts_of_the_full_flow(self):
        self.zt = support.fresh(self)
        c = _FakeCTimeouts()
        waits = []  # timeouts used to wait for the end event
        patches = [
            mock.patch.object(zc_reporting, "_ctx_tasks", lambda n=30: [
                {"sid": support.T2, "ws": support.PROJ_CV, "title": "CV"}]),
            mock.patch.object(zc_reporting.zc_proto, "make",
                              lambda cwd=None, **kw: c),
            mock.patch.object(zc_reporting, "_model_snapshot",
                              lambda sid: None),
            mock.patch.object(zc_reporting, "_wait_cli_event",
                              lambda sid, t0, evs, timeout:
                              (waits.append(timeout),
                               {"event": "compact.completed",
                                "durationMs": 90000})[1]),
            mock.patch.object(zc_reporting, "_live_skip_clear",
                              lambda sid: None),
            mock.patch.object(zc_core, "transcript_mb", return_value=25.0),
            mock.patch.object(zc_core, "_context_ktokens",
                              return_value=300.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = zc_reporting.zcode_compact(sid=support.T2)
        self.assertIn("Compaction of «CV» done", resp)
        t_mb = 60 + int(25.0 * 4)          # 160 — resume and setModel
        t_comp = min(600, max(300, int(300 * 1.2)))  # 360 — the compact
        self.assertEqual(c.seen, [
            ("session/list", 30),
            ("session/resume", t_mb),
            ("session/setModel", t_mb),
            ("session/compact", t_comp),
            ("session/messages", 30)])
        # the end event is awaited with the compact budget +30
        self.assertEqual(waits, [t_comp + 30])

    def test_early_ack_your_choice_arrived(self):
        """F35 — Damian's request: two feedbacks on a button tap.
        Telegram's native 👀 (answerCallbackQuery) covers the first;
        this is the app's one: as soon as session/list confirms idle,
        «📬 Your choice arrived» goes out BEFORE the heavy resume and the
        final report — the phone no longer stays mute for the minutes
        the compaction takes."""
        self.zt = support.fresh(self)
        c = _FakeCTimeouts()
        sends = []
        patches = [
            mock.patch.object(zc_reporting, "_ctx_tasks", lambda n=30: [
                {"sid": support.T2, "ws": support.PROJ_CV, "title": "CV"}]),
            mock.patch.object(zc_reporting.zc_proto, "make",
                              lambda cwd=None, **kw: c),
            mock.patch.object(zc_reporting, "_model_snapshot",
                              lambda sid: None),
            mock.patch.object(zc_reporting, "_wait_cli_event",
                              lambda sid, t0, evs, timeout:
                              {"event": "compact.completed",
                               "durationMs": 90000}),
            mock.patch.object(zc_reporting, "_live_skip_clear",
                              lambda sid: None),
            mock.patch.object(zc_core, "transcript_mb", return_value=25.0),
            mock.patch.object(zc_core, "_context_ktokens",
                              return_value=300.0),
            mock.patch.object(zc_reporting, "load_config",
                              lambda: {"token": "T", "chat_id": 1}),
            mock.patch.object(zc_reporting.uhttp, "send",
                              lambda cfg, text, loud=None, markup=None, a_outbox=True:
                                  (sends.append(text), True)[1]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = zc_reporting.zcode_compact(sid=support.T2)
        # the final report is RETURNed (the dispatcher sends it) and the
        # ack was the flow's ONLY send: zero silence, no noise either
        self.assertIn("Compaction of «CV» done", resp)
        self.assertEqual(len(sends), 1)
        self.assertIn("Your choice arrived", sends[0])
        self.assertIn("«CV»", sends[0])


if __name__ == "__main__":
    unittest.main()
