"""F77 — the chat's two MODES and the handshake that DOES itself.

27/10, after a morning of one pointer for two sessions: Damian designed
the split himself (typing in lector mode answers "reader mode"; lector
is the DEFAULT) — lector = the chat only watches, /task moves what
streams; escritor = free text runs in the active conversation (queued
while the Mac is mid-turn — they never collide). And the thinking line
is ALWAYS on for the current turn: the quiet-when-k left every 🔧
state invisible in the app-conversation sessions (his report: no
thinking of any kind — edit, read, cot...).

/handshake reports the pipe AND performs the missing handshake (his
words: it must also PRODUCE the effect of handshaking if it is not
done yet): a focus running with a dead watcher is re-attached there.
"""
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import core as zc_core  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
from zcode_bot.commands import callbacks as cb  # noqa: E402
from zcode_bot.commands import handlers_system as hs  # noqa: E402
from zcode_bot.commands import handlers_tasks as ht  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
T0 = int(time.time()) - 60
SID = "sess_f77c0de5-1111-4222-8333-9f77a0dee0f5"
CAP = reasoning.COT_BOX_CAP
LECTOR, ESCRITOR = zc_core.MODE_LECTOR, zc_core.MODE_ESCRITOR


def _msg(role, *parts):
    return {"info": {"role": role}, "parts": list(parts)}


def _r(text):
    return {"type": "reasoning", "text": text}


def _act(**kw):
    d = {"sid": SID, "title": "f77", "ws": "/w"}
    d.update(kw)
    return d


class TestBotMode(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_the_default_is_lector(self):
        # a session file with NO bot_mode (every pre-F77 record) reads
        # as lector — and the round trip keeps the key out until set
        zc_core.set_active_bot_session(_act())
        act = zc_core.active_bot_session()
        self.assertEqual(zc_core.bot_mode_of(act), LECTOR)
        self.assertNotIn("bot_mode", act.to_dict())
        self.assertEqual(zc_core.bot_mode_of({}), LECTOR)

    def test_lector_refuses_free_text(self):
        zc_core.set_active_bot_session(_act())   # no bot_mode → lector
        spawns = []
        support.patch(self, ht, "_spawn_worker",
                      lambda *a, **kw: spawns.append(a) or True)
        r = ht.handle_free_text(CFG, "run this")
        self.assertIn("Reader mode", r)
        self.assertIn("/bot_mode %s" % ESCRITOR, r)
        self.assertEqual(spawns, [])   # NOTHING runs: the chat watches

    def test_escritor_runs_the_text(self):
        zc_core.set_active_bot_session(_act(bot_mode=ESCRITOR))
        support.patch(self, ht, "running_turns", lambda: {})
        support.patch(self, ht, "_spawn_worker",
                      lambda cfg, act, text, **kw: True)
        r = ht.handle_free_text(CFG, "run this")
        self.assertIn("working (headless)", r)

    def test_a_question_still_answers_in_lector(self):
        # the agent is WAITING for the answer — answering cannot collide
        zc_core.set_active_bot_session(_act())
        answered = []

        class _Q:
            def first_pending(self, sid):
                return {"request_id": "r1",
                        "questions": [{"options": [{"value": "si"},  # noqa: spanish — legacy bilingual yes
                                                   {"value": "no"}]}]}

            def answer(self, rid, resp):
                answered.append((rid, resp))

        support.patch(self, ht, "_questions", lambda: _Q())
        r = ht.handle_free_text(CFG, "2")
        self.assertIn("Answered", r)
        self.assertEqual(answered, [("r1", "no")])

    def test_the_buttons_and_the_callback(self):
        r = hs._cmd_bot_mode(CFG, "")
        text, kb = r
        self.assertIn(LECTOR, text)
        datas = [b["callback_data"] for row in kb["inline_keyboard"]
                 for b in row]
        self.assertEqual(sorted(datas), sorted(["bm:" + ESCRITOR, "bm:" + LECTOR]))
        self.assertEqual(cb.callback_text("bm:" + LECTOR), "/bot_mode " + LECTOR)
        self.assertEqual(cb.callback_text("bm:" + ESCRITOR),
                         "/bot_mode " + ESCRITOR)

    def test_set_round_trip_and_invalid(self):
        hs._cmd_bot_mode(CFG, ESCRITOR)
        self.assertEqual(zc_core.active_bot_session().bot_mode, ESCRITOR)
        self.assertIn(LECTOR, hs._cmd_bot_mode(CFG, LECTOR))
        self.assertEqual(zc_core.active_bot_session().bot_mode, LECTOR)
        self.assertIn("⚠️", hs._cmd_bot_mode(CFG, "bogus"))
        with open(os.path.join(zc_core.config.STATE_DIR,
                               "bot-session.json")) as f:
            self.assertEqual(json.load(f)["bot_mode"], LECTOR)

    def test_the_pick_reply_knows_the_mode(self):
        from zcode_bot import agent_live
        support.patch(self, ht, "_ctx_tasks",
                      lambda n: [{"sid": SID, "title": "f77", "ws": "/w"}])
        support.patch(self, agent_live, "stop_views", lambda: None)
        support.patch(self, ht, "running_turns", lambda: {})
        support.patch(self, ht, "_queue_resume", lambda *a, **kw: "")
        support.patch(self, ht, "load_config", lambda: CFG)
        zc_core.set_active_bot_session(_act())        # lector (default)
        self.assertIn(LECTOR, ht._task_select_sid(SID))
        zc_core.set_active_bot_session(_act(bot_mode=ESCRITOR))
        self.assertIn("runs there", ht._task_select_sid(SID))


class TestHandshake(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        zc_core.set_active_bot_session(_act(focus_sid=SID))
        support.patch(self, hs.uhttp, "tg_api",
                      lambda cfg, method, params=None, timeout=35:
                      {"ok": True})
        self.attached = []
        from zcode_bot import attach as zc_attach
        support.patch(self, zc_attach, "mac_attach",
                      lambda cfg, sid, title, ws, deps=None:
                      self.attached.append((sid, title, ws)) or
                      "cot=live spec=live")

    def _marker(self):
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")

    def test_reports_the_pipe_without_touching_it(self):
        self._marker()
        support.patch(self, reasoning, "watcher_alive", lambda sid: True)
        r = hs._cmd_handshake(CFG, "")
        for frag in ("handshake", "Telegram: 🟢", "Mode: lector",
                     "Mac turn: 🟢", "Watcher cot-live: 🟢",
                     "Sidecar live-io: 🔴", "does not flush"):
            self.assertIn(frag, r, r)
        self.assertEqual(self.attached, [])   # healthy: no re-attach

    def test_it_does_the_handshake_when_the_watcher_is_dead(self):
        self._marker()
        support.patch(self, reasoning, "watcher_alive", lambda sid: False)
        r = hs._cmd_handshake(CFG, "")
        self.assertIn("Watcher cot-live: 🔴", r)
        self.assertIn("Re-attached", r)
        self.assertEqual(self.attached, [(SID, "f77", "/w")])

    def test_no_running_turn_no_reattach(self):
        support.patch(self, reasoning, "watcher_alive", lambda sid: False)
        r = hs._cmd_handshake(CFG, "")
        self.assertIn("Mac turn: — none", r)
        self.assertEqual(self.attached, [])

    def test_the_sidecar_is_green_when_fresh(self):
        self._marker()
        support.patch(self, reasoning, "watcher_alive", lambda sid: True)
        from zcode_bot import live_io
        p = live_io.sidecar_path(SID)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{}\n")
        r = hs._cmd_handshake(CFG, "")
        self.assertIn("Sidecar live-io: 🟢", r)

    def test_network_errors_of_today_are_counted(self):
        from zcode_bot import config
        with open(os.path.join(config.LOG_DIR, "errors.log"), "w") as f:
            f.write("[%s 10:00:00] sendMessage failed: reset\n"
                    % time.strftime("%Y-%m-%d"))
            f.write("[2000-01-01 10:00:00] old: ignored\n")
        r = hs._cmd_handshake(CFG, "")
        self.assertIn("Network today: 1", r)


class TestLlAlwaysOn(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_the_thinking_line_shows_with_the_store_streaming(self):
        """The old quiet-when-k: with the store streaming the part in
        progress (k), the thinking line never appeared — every tool
        state invisible in the app-conversation sessions. Now the
        cajita carries the CoT and the line carries the STATUS."""
        self.sends, self.edits, self.kb = [], [], []
        support.patch(self, reasoning.uhttp, "send",
                      lambda cfg, text, loud=None, markup=None:
                      (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((ph, text, markup)), True)[1])
        support.patch(self, reasoning, "pwlog",
                      lambda *a: self.kb.append(" ".join(str(x) for x in a)))
        support.patch(self, reasoning.time, "sleep", lambda s: None)
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")

        class _ProtoStreamingTool:
            """The store holds an in-progress step with a RUNNING Edit
            tool part — k non-empty forever, until the marker dies."""

            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 3:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": [
                        _msg("user", {"type": "text", "text": "p"}),
                        _msg("assistant",
                             {"type": "tool", "tool": "Edit",
                              "state": {"status": "running",
                                        "input": {"file_path":
                                                  "/w/reasoning.py"}}}),
                    ]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoStreamingTool(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "f77", T0, "/w")
        # the line was BORN while k was non-empty — with the tool's own
        # vocabulary — and its birth is in the log (auditable at last)
        line = [t for t, _m in self.sends if "Editing" in t
                and "reasoning.py" in t]
        self.assertTrue(line, self.sends)
        self.assertTrue(any("mac ll born" in ln for ln in self.kb),
                        self.kb)
        self.assertTrue(any("thinking" in t or "Editing" in t
                            for _ph, t, _m in self.edits), self.edits)
