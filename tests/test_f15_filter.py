"""F15 — only the current task talks to the chat.

Damian's request 2026-10-13: "I want updates, messages and notices to
arrive ONLY from the TASK CURRENT, nothing from the rest". The system
had NOT ONE session filter: hooks fired for any runtime (including the
app running other tasks), every phone turn produced a double stream
(hooks ▶️/✅ ON TOP of the worker's boxes — verified in payloads with
HEADLESS_GUARD), a mid-turn /task switch didn't cut the stream, and the
queue relaunched turns of sessions that had stopped being the active
one.

Rules covered:
- unsolicited push from another task → silence (the marker still gets
  cleaned up).
- permission/question from another task → neither message NOR wait (the
  Mac's dialogue wins; before it held the tool call for up to 240 s).
- session with its own worker → hooks silenced (the worker already
  reports).
- the turn's final reply → ALWAYS arrives (not tested here: it's
  prompt_worker's rule, the gate lives only in _live_view).
- an inactive task's queue sleeps; /task returns → auto-resume.
- ❓ questions routed by sid (the current task first).
"""
import json
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

import support
from zcode_bot.limites import CH_TURNO  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

from zcode_bot import concurrency  # noqa: E402


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # F4j: the on_* live in hooks — ALL stubs go to the owning module
        # (the transport, to the shared zc_http seam)
        from zcode_bot import hooks as zc_hooks
        from zcode_bot.utils import http as zc_http
        self.zt = support.fresh(self)
        self.T1 = support.T1  # active by default (bot-session.json fixture)
        self.T2 = support.T2  # CV — another task
        self.sends = []
        self.emitted = []
        # on_user_prompt must not spawn real stuck-watch processes
        self.popen = []
        self.wait_calls = []

        def fake_wait(cfg, hint):
            self.wait_calls.append(hint)
            return ("yes", "si")
        patches = [
            mock.patch.object(
                zc_http, "send",
                lambda cfg, text, loud=None, markup=None:
                    (self.sends.append(text), True)[1]),
            mock.patch.object(zc_http, "_live_send", lambda cfg, text: 555),
            mock.patch.object(
                zc_http, "_live_edit",
                lambda cfg, mid, text:
                    (self.sends.append(("edit", text)), True)[1]),
            mock.patch.object(zc_hooks, "emit",
                              lambda obj: self.emitted.append(obj)),
            mock.patch.object(
                zc_hooks, "subprocess", types.SimpleNamespace(
                    Popen=lambda *a, **k: self.popen.append(a),
                    DEVNULL=subprocess.DEVNULL)),
            mock.patch.object(zc_hooks, "wait_reply", fake_wait),
        ]
        for pp in patches:
            pp.start()
            self.addCleanup(pp.stop)

    def _p(self, sid, **kw):
        p = {"sessionId": sid, "cwd": "/fake/proj"}
        p.update(kw)
        return p


class TestHooksOtherTask(_Base):
    """Push from a task that is NOT the current one: total silence."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)

    async def test_foreign_prompt_does_not_notify_but_marker_yes(self):
        zt = self.zt
        zt.on_user_prompt(self.cfg, self._p(self.T2, prompt="hello CV"))
        self.assertEqual(self.sends, [])
        # the stuck-watch DOES start (if the task becomes current again
        # and is stuck, it notifies): what's gated is the send, not the
        # marker cycle
        self.assertEqual(len(self.popen), 1)
        self.assertTrue(os.path.exists(zt.marker_path(self.T2)))
        zt.on_stop(self.cfg, self._p(self.T2, responsePreview="done"))
        self.assertEqual(self.sends, [])
        self.assertFalse(os.path.exists(zt.marker_path(self.T2)))

    async def test_failure_and_session_foreign_silenced(self):
        zt = self.zt
        zt.on_tool_failure(self.cfg, self._p(self.T2, tool_name="Bash",
                                             error="weird segfault"))
        zt.on_session_start(self.cfg, self._p(self.T2, source="resume"))
        self.assertEqual(self.sends, [])

    async def test_foreign_permission_no_send_no_wait(self):
        # before: 🔐 + wait_reply holding the tool call for up to 240 s for
        # a task that wasn't even being watched (and competing for getUpdates)
        self.zt.on_permission_request(self.cfg, self._p(self.T2))
        self.assertEqual(self.sends, [])
        self.assertEqual(self.wait_calls, [])

    async def test_foreign_question_does_not_notify(self):
        self.zt.on_ask_user_question(self.cfg, self._p(
            self.T2, tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "Sure?"}]}))
        self.assertEqual(self.sends, [])

    async def test_without_sessionid_silences(self):
        self.zt.on_session_start(self.cfg, {"source": "startup"})
        self.assertEqual(self.sends, [])

    async def test_without_active_task_silences(self):
        self.zt.set_active_bot_session({})
        self.zt.on_session_start(self.cfg, self._p(self.T2, source="startup"))
        self.assertEqual(self.sends, [])

    async def test_flag_turns_the_filter_off(self):
        cfg = _cfg(self.zt, only_active_task=False)
        self.zt.on_session_start(cfg, self._p(self.T2, source="startup"))
        self.assertEqual(len(self.sends), 1)  # previous behavior


class TestHooksTaskCurrent(_Base):
    """The current task (running in the app, no bot worker) does notify."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)

    async def test_prompt_notifies_and_spawns_stuck_watch(self):
        zt = self.zt
        zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="fix X"))
        self.assertEqual(len(self.sends), 1)
        self.assertIn("Turn started", self.sends[0])
        # stuck-watch + spectator (F33: the Mac's turn streams to the phone)
        self.assertEqual(len(self.popen), 2)
        zt.on_stop(self.cfg, self._p(self.T1, responsePreview="done"))
        self.assertEqual(len(self.sends), 2)
        self.assertIn("Turn finished", self.sends[1])


class TestFullTurn(_Base):
    """F32 — Damian's request: the Mac's turn arrives COMPLETE on the phone
    (full prompt and reply, split at Telegram's limit), with task name,
    steps, duration and workspace in the header."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)

    def test_full_prompt_in_parts(self):
        prompt = "x" * 5000
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt=prompt))
        self.assertEqual(len(self.sends), 2)  # 5000 chars → 2 parts
        self.assertIn("Turn started", self.sends[0])
        self.assertTrue(self.sends[0].endswith("(1/2)"))
        joined = (self.sends[0].split("\n", 2)[2].rsplit(" (", 1)[0]
                   + self.sends[1].split("\n", 1)[1].rsplit(" (", 1)[0])
        self.assertEqual(joined, prompt)

    def test_full_reply_with_data(self):
        resp = "y" * 9000
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="get going"))
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText=resp,
                                          toolCallCount=3))
        # 1 from the ▶️ + 3 from the reply (9000 / CH_TURNO)
        self.assertEqual(len(self.sends), 4)
        end = self.sends[1]
        self.assertIn("Turn finished", end)
        self.assertIn("🧰 3 steps", end)
        self.assertIn("⏱ 0 min", end)  # marker with no age → 0
        self.assertIn("📁fake/proj", end)  # parent/child since F33e
        body = "".join(s.split("\n", 2)[-1].rsplit(" (", 1)[0]
                         for s in self.sends[1:])
        self.assertEqual(body, resp)

    def test_duration_from_marker(self):
        import time as _t
        marker = self.zt.marker_path(self.T1)
        with open(marker, "w") as f:
            f.write("{}")
        old = _t.time() - 130
        os.utime(marker, (old, old))
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText="done"))
        self.assertIn("⏱ 2 min", self.sends[0])

    def test_cut_after_max_parts(self):
        resp = "z" * (CH_TURNO * 8)  # 8 parts > 6 allowed
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText=resp))
        self.assertEqual(len(self.sends), 6)
        self.assertIn("(+", self.sends[-1])
        self.assertIn("/cot", self.sends[-1])

    def test_without_reply_notifies(self):
        self.zt.on_stop(self.cfg, self._p(self.T1))
        self.assertEqual(len(self.sends), 1)
        self.assertIn("(no response)", self.sends[0])

    def test_spectator_spawns_for_current(self):
        # F33: the Mac's turn streams to the phone (tool boxes + narration)
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        self.assertTrue(any("--espectador" in a[0] for a in self.popen))

    def test_spectator_without_knob_does_not_spawn(self):
        self.cfg["mac_spectator"] = False
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        self.assertFalse(any("--espectador" in a[0] for a in self.popen))
        self.assertEqual(len(self.popen), 1)  # stuck-watch unchanged

    def test_mac_origin_explicit(self):
        # Damian's request: tell the Mac's turns apart from the bot's
        # (the workspace may be called "bot" and cause confusion)
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        self.assertIn("🖥️ Mac", self.sends[0])
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText="done"))
        self.assertIn("🖥️ Mac", self.sends[-1])

    def test_workspace_with_parent_no_mixing(self):
        # a bare "📁bot" read as origin: "Mac - Bot" (19/10) — with the
        # parent/child path it's visible that it's a folder
        self.zt.on_stop(self.cfg, self._p(
            self.T1, responseText="x", cwd="/Users/lattenero/Projects/bot"))
        self.assertIn("📁Projects/bot", self.sends[-1])
        self.assertNotIn("📁bot ", self.sends[-1])

    async def test_failure_and_session_notify(self):
        zt = self.zt
        zt.on_tool_failure(self.cfg, self._p(self.T1, tool_name="Bash",
                                             error="weird segfault"))
        zt.on_session_start(self.cfg, self._p(self.T1, source="resume"))
        self.assertEqual(len(self.sends), 2)

    async def test_current_permission_waits_and_approves(self):
        zt = self.zt
        zt.on_permission_request(self.cfg, self._p(
            self.T1, tool_name="Bash", tool_input={"command": "ls"}))
        self.assertTrue(any("Approval needed" in s for s in self.sends))
        self.assertEqual(self.wait_calls, ["pending approval"])
        dec = self.emitted[0]["hookSpecificOutput"]["decision"]["behavior"]
        self.assertEqual(dec, "allow")

    async def test_current_question_notifies_mac_mode(self):
        self.zt.on_ask_user_question(self.cfg, self._p(
            self.T1, tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "Front or back?"}]}))
        self.assertEqual(len(self.sends), 1)
        self.assertIn("The agent asks", self.sends[0])
        self.assertEqual(self.wait_calls, [])  # mac mode: notifies and returns


class TestWorkerSinDobleStream(_Base):
    """Session with a bot worker: hooks do NOT duplicate its reporting."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)
        self.zt._workers().register(self.T1, {"t0": time.time(), "titulo": "testing"})

    async def test_all_hooks_silenced(self):
        zt = self.zt
        zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        zt.on_stop(self.cfg, self._p(self.T1, responsePreview="y"))
        zt.on_session_start(self.cfg, self._p(self.T1, source="resume"))
        zt.on_permission_request(self.cfg, self._p(self.T1, tool_name="Bash"))
        zt.on_ask_user_question(self.cfg, self._p(
            self.T1, tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "?"}]}))
        self.assertEqual(self.sends, [])
        self.assertEqual(self.popen, [])
        self.assertEqual(self.wait_calls, [])
        # the marker still lives its cycle (side-effect ≠ notification)
        self.assertFalse(os.path.exists(zt.marker_path(self.T1)))


class TestTaskCurrentHelper(_Base):
    """_task_current_ok: validity is read LIVE (a /task switch cuts it)."""

    async def test_basic_rules(self):
        zt = self.zt
        cfg = _cfg(zt)
        self.assertTrue(zt._task_current_ok(cfg, self.T1))
        self.assertFalse(zt._task_current_ok(cfg, self.T2))
        self.assertFalse(zt._task_current_ok(cfg, None))
        self.assertTrue(zt._task_current_ok(_cfg(zt, only_active_task=False),
                                            self.T2))

    async def test_the_switch_cuts_live(self):
        zt = self.zt
        cfg = _cfg(zt)
        self.assertTrue(zt._task_current_ok(cfg, self.T1))
        zt.set_active_bot_session({"sid": self.T2, "title": "CV",
                                   "ws": support.PROJ_CV})
        self.assertFalse(zt._task_current_ok(cfg, self.T1))
        self.assertTrue(zt._task_current_ok(cfg, self.T2))


class _FakeClient:
    def __init__(self):
        self.max_frame = 0

    def request(self, method, params=None, timeout=None):
        return {"result": {"events": []}}


class _Th:
    """Simulates the turn-end poller: dies after N checks."""
    
    def __init__(self, rounds=3):
        self.n = rounds
    
    def is_alive(self):
        self.n -= 1
        return self.n > 0


class _ThFast:
    """Fast version for tests: 1 lap, no sleep."""
    
    def __init__(self):
        self.done = False
    
    def is_alive(self):
        if self.done:
            return False
        self.done = True
        return True


class TestLiveViewPausa(_Base):
    """On ceasing to be the current task mid-turn: ONE ⏸️ edit and zero
    stream (the final reply is still sent by prompt_worker).

    F4d: against LiveTurnView with injected deps — without patching the
    global time.sleep (the seam is now the constructor).
    """

    def _run(self):
        from zcode_bot.live_view import LiveTurnView
        sends = []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (sends.append(text), True)[1],
            "live_send": lambda cfg, text: 555,
            "live_edit": lambda cfg, mid, text:
                (sends.append(("edit", text)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c, sid, after_id=None, limit=40: [],
            "poll_cadence": lambda mf: 0.0,
            "rss": lambda: -1,
            "rss_now": lambda: -1,
        }
        import time as _t
        return sends, LiveTurnView(
            _FakeClient(), _cfg(self.zt), self.T1, _t.time(), "testing",
            777, _ThFast(), last_id=None, deps=deps).run()

    async def test_pause_with_a_single_edit(self):
        self.zt.set_active_bot_session({"sid": self.T2, "title": "CV",
                                        "ws": support.PROJ_CV})
        sends, _lr = self._run()
        edits = [s for s in sends if isinstance(s, tuple)]
        plain = [s for s in sends if not isinstance(s, tuple)]
        self.assertEqual(plain, [])          # no CoT or notices
        self.assertEqual(len(edits), 1)         # ONE farewell, not a shower
        self.assertIn("background", edits[0][1])

    async def test_without_pause_keeps_streaming(self):
        sends, _lr = self._run()
        edits = [s for s in sends if isinstance(s, tuple)]
        self.assertGreaterEqual(len(edits), 1)
        self.assertNotIn("background", edits[0][1])


class TestDormantQueueAndResume(_Base):
    """An inactive task's queue sleeps; /task takes it back on its own."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)
        self.act1 = {"sid": self.T1, "title": "testing", "ws": support.PROJ_BOT}
        self.act2 = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.fired = threading.Event()
        self.launched = []

        def spy_spawn(cfg, act, text):
            self.launched.append((act.get("sid"), text))
            self.fired.set()
            return object()  # spawn "ok"
        # F4g: _queue_launch_next/_queue_resume live in workers — the spy
        # covers both modules (the gateway still consumes in handle_free_text)
        support.patch(self, self.zt, "_spawn_worker", spy_spawn)
        from zcode_bot import workers as zc_workers
        pw = mock.patch.object(zc_workers, "_spawn_worker", spy_spawn)
        pw.start()
        self.addCleanup(pw.stop)

    async def test_drain_does_not_relaunch_inactive_session(self):
        zt = self.zt
        from zcode_bot import workers as zc_workers
        launches = []
        pp = mock.patch.object(zc_workers, "_queue_launch_next",
                               lambda cfg, sid: launches.append(sid))
        pp.start()
        self.addCleanup(pp.stop)
        zt._queue_put(self.T2, self.act2, "continue with the CV")
        zt._queue_drain(self.cfg, self.T2)
        self.assertEqual(launches, [])
        self.assertEqual(len(zt._queue_peek(self.T2)), 1)  # intact

    async def test_drain_relaunches_the_active_one(self):
        zt = self.zt
        # T1 is RUNNING in the fixture (busy) → use T2 (completed): the
        # drain only relaunches free and current sessions (F17)
        zt.set_active_bot_session(self.act2)
        zt._queue_put(self.T2, self.act2, "continue")
        zt._queue_drain(self.cfg, self.T2)
        self.assertTrue(self.fired.wait(3))
        self.assertEqual(self.launched, [(self.T2, "continue")])

    async def test_resume_launches_and_notifies(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)
        zt._queue_put(self.T2, self.act2, "one")
        zt._queue_put(self.T2, self.act2, "two")
        resp = zt._queue_resume(self.cfg, self.T2, "CV")
        self.assertTrue(self.fired.wait(3))
        self.assertEqual(self.launched, [(self.T2, "one")])
        self.assertEqual(len(zt._queue_peek(self.T2)), 1)
        self.assertIn("Resumed", resp)
        self.assertIn("1 more queued", resp)

    async def test_resume_with_live_worker_does_not_duplicate(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)
        zt._workers().register(self.T2, {"t0": time.time(), "titulo": "CV"})
        zt._queue_put(self.T2, self.act2, "one")
        zt._queue_put(self.T2, self.act2, "two")
        resp = zt._queue_resume(self.cfg, self.T2, "CV")
        self.assertFalse(self.fired.is_set())
        self.assertIn("turn is already running", resp)
        self.assertEqual(len(zt._queue_peek(self.T2)), 2)

    async def test_task_select_integra_resume(self):
        zt = self.zt
        zt._queue_put(self.T2, self.act2, "one from the CV")
        reply = zt._task_select_sid(self.T2)
        self.assertIn("🎯", reply)
        self.assertTrue(self.fired.wait(3))
        self.assertIn("Resumed", reply)


class TestQuestionsBySid(unittest.IsolatedAsyncioTestCase):
    """Concurrency: the reply goes to the session that asked (F16: via
    files — QuestionFiles, the worker↔poller channel)."""

    def setUp(self):
        import tempfile
        self.broker = concurrency.QuestionFiles(
            tempfile.mkdtemp(prefix="zcq15-"))

    async def test_priority_by_sid(self):
        b = self.broker
        b.ask("r1", [{"question": "A?"}], "T1", sid=support.T1)
        b.ask("r2", [{"question": "B?"}], "T2", sid=support.T2)
        self.assertEqual(b.first_pending(support.T2)["request_id"], "r2")
        self.assertEqual(b.first_pending(support.T1)["request_id"], "r1")
        # sid with no question of its own → fallback to the first one (previous behavior)
        self.assertEqual(b.first_pending("sess_OTRA")["request_id"], "r1")
        self.assertEqual(b.first_pending()["request_id"], "r1")
        self.assertTrue(b.answer("r2", "option 2"))
        remaining = b.pendings()
        self.assertEqual([q["request_id"] for q in remaining], ["r1"])

    async def test_sid_default_none_backward_compatible(self):
        b = self.broker.ask("r1", [{"question": "A?"}], "T1")
        self.assertIsNone(b["sid"])
        self.assertEqual(
            self.broker.first_pending(support.T1)["request_id"], "r1")


if __name__ == "__main__":
    unittest.main()
