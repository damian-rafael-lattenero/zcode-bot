# -*- coding: utf-8 -*-
"""E2E — the COMPLETE journey of a turn, no patched stages in the middle.

Born from Damian's healthy skepticism post-F49 (20/10: «you say it
works? write several e2e tests and check the whole bot works well»):
the suite covered the stages SEPARATELY (the worker happy-path with a
fake LiveTurnView, the LiveTurnView with a fake gateway) and the whole
journey was only ever tested live. The burned-in lesson: verifying
internal state isn't enough for chat UX — you must assert the SEQUENCE
of messages the owner sees.

Faked (the system's edges, nothing in the middle):
  - the zcode runtime: a fake ProtoClient delivering the turn's
    events in batches (one per poll), honoring afterSeq for real;
  - Telegram: a fake chat that records every send/edit in chronological
    order and hands out increasing message_ids (what ph_of needs: ints).

Everything else is production code: prompt_worker (spawn, proto,
turn, delivery, finally), the REAL LiveTurnView (no injected deps), the
sealer, razonamiento (cache + kb_log → fixture pwlog), the
gateway and command dispatch.

E1  multi-reasoning CoT turn — the button-bug scenario: each
    reasoning in ITS message with ITS buttons, close without a global index
E2  taps on the cache left by E1 — the viewer edits just that message
    with the buttons that LIVE in it (F48b/F49 regression)
E3  turn without CoT — the classic flow intact + the «sin CoT» notice (F45)
E4  day-to-day commands via process_update_sync, real dispatch
E5  stragglers — the 2nd reasoning's send fails → ONE late-comers message
"""
import glob
import json
import os
import threading
import time
import unittest
from unittest import mock

import support

from zcode_bot import workers as zc_workers
from zcode_bot.core import set_active_bot_session
from zcode_bot.utils import http as zc_http


# ------------------------------------------------------------- fakes

def _with_seqs(batches):
    """[[ (kind, delta), ... ], ...] → a list of EVENTS per batch, with
    seq/eventId incrementing from 101 (100 is the eventSeq the
    fake's subscribe returns)."""
    seq = 100
    out = []
    for batch in batches:
        evs = []
        for kind, delta in batch:
            seq += 1
            p = {"kind": kind}
            if delta:
                p["delta"] = delta
            evs.append({"seq": seq, "eventId": "e%d" % seq, "payload": p})
        out.append(evs)
    return out


class FakeRuntime:
    """Fake ProtoClient: the whole turn as the stream sees it.

    session/events delivers ONE batch per call (LiveTurnView polls
    every ~2 s); when they run out, the first EMPTY poll sets `done` —
    that way the fake _turn_end_poll (which waits for `done`) only
    kills the th once the view has PROCESSED the last batch: no races,
    no swallowed events."""

    def __init__(self, batches, reply="final reply of the turn"):
        self.batches = _with_seqs(batches)
        self.reply = reply
        self.requests = []
        self.closed = False
        self.done = threading.Event()

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        if method == "session/events":
            after = (params or {}).get("afterSeq")
            if self.batches:
                batch = self.batches.pop(0)
                evs = [e for e in batch
                       if after is None or e["seq"] > after]
            else:
                self.done.set()
                evs = []
            return {"result": {"events": evs}}
        if method == "session/messages":
            return {"result": {"messages": [
                {"info": {"role": "user", "id": "m1"},
                 "parts": [{"type": "text", "text": "do something"}]},
                {"info": {"role": "assistant", "id": "m2"},
                 "parts": [{"type": "text", "text": self.reply}]}]}}
        if method == "session/subscribe":
            return {"result": {"eventSeq": 100}}
        return {"result": {}}

    def close(self):
        self.closed = True


class ChatTelegram:
    """Fake Telegram: records every send/edit IN ORDER (what the owner
    sees) with its message_id and markup. `mark_results` schedules
    what to return for sends WITH markup (E5: None = the send failed)."""

    def __init__(self):
        self.log = []            # ("send"|"edit", mid, text, markup)
        self._mid = 699
        self.mark_results = []

    def _next(self):
        self._mid += 1
        return self._mid

    def send(self, cfg, text, loud=None, markup=None):
        mid = self._next()
        self.log.append(("send", mid, text, markup))
        if markup is not None and self.mark_results:
            r = self.mark_results.pop(0)
            return mid if r is True else r
        return mid

    def live_send(self, cfg, text):
        mid = self._next()
        self.log.append(("send", mid, text, None))
        return mid

    def live_edit(self, cfg, mid, text, markup=None):
        self.log.append(("edit", mid, text, markup))
        return True

    def sends(self):
        return [e for e in self.log if e[0] == "send"]

    def edits(self):
        return [e for e in self.log if e[0] == "edit"]

    @staticmethod
    def buttons(e):
        kb = e[3] or {}
        return [b["text"] for fila in kb.get("inline_keyboard", [])
                for b in fila]


# Damian's scenario: 2 reasonings (3 boxes in the 1st, 1 in the 2nd),
# a tool in the middle and text at the end — the turn that piled up buttons.
BATCHES_2PASSES = [
    [("reasoning_delta", "x" * 2000), ("reasoning_delta", "y" * 1500)],
    [("tool_input_start", ""), ("tool_call", "")],
    [("reasoning_delta", "z" * 300)],
    [("text_delta", "I write the reply")],
]
# x2000 + y1500 = 3500 → box 1 (3000) fills LIVE; the tail (y*500) is
# sealed when the tool closes r1; r2 (z*300) closes on writing → 2.1.


class _E2EBase(unittest.TestCase):
    """Fresh environment + fake Telegram/runtime. The worker, the view,
    the sealer, razonamiento and the gateway run FOR REAL."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.chat = ChatTelegram()
        for mod, name, val in (
            (zc_http, "send", self.chat.send),
            (zc_http, "_live_send", self.chat.live_send),
            (zc_http, "_live_edit", self.chat.live_edit),
            (self.zt, "send", self.chat.send),      # the gateway's reply
            (self.zt, "react", lambda cfg, msg, e: None),
            (self.zt, "typing", lambda cfg: None),
        ):
            p = mock.patch.object(mod, name, val)
            p.start()
            self.addCleanup(p.stop)

    def _run_turn(self, batches, reply="final reply of the turn",
                  **cfg_extra):
        """Runs the REAL prompt_worker on FakeRuntime; returns the
        runtime (to inspect requests/close)."""
        runtime = FakeRuntime([list(t) for t in batches], reply)

        def _end(sid, t0, **kw):
            runtime.done.wait(30)   # the view already processed the last batch
            time.sleep(0.05)         # and completed its last poll
            return {"event": "turn.completed"}

        patches = [
            mock.patch.object(zc_workers, "zc_proto", type("M", (), {
                "make": staticmethod(lambda cwd=None, **kw: runtime),
                "proto_err": __import__(
                    "zcode_bot.proto", fromlist=["proto_err"]).proto_err})),
            mock.patch.object(zc_workers, "_turn_end_poll", _end),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1)
        cfg.update(cfg_extra)
        # the fixture turns cot_botones off for the whole suite: here it's
        # the production bot's default (each test can turn it off)
        cfg["cot_botones"] = cfg_extra.get("cot_botones", True)
        # T2: its fixture turn is COMPLETED (T1 has an open
        # turn.started → running_turns reports it busy → busy).
        # We also declare it active, as a real /task n would — the
        # only_active_task filter silences every other stream.
        act = {"sid": support.T2, "title": "CV", "ws": support.PROJ_CV}
        set_active_bot_session(act)
        zc_workers.prompt_worker(cfg, act, "do something with several tools")
        return runtime

    def _pwlog(self):
        p = os.path.join(self.zt.config.BASE, "hooks", "logs",
                         "prompt-worker.log")
        with open(p) as f:
            return f.read()

    def _cache(self):
        """The turn cache's (dict, t0) — no guessing t0: read from disk."""
        pat = os.path.join(self.zt.config.STATE_DIR,
                           "cot-%s-*.json" % support.T2)
        files = glob.glob(pat)
        self.assertEqual(len(files), 1, "one cache per turn: %s" % files)
        pref = "cot-%s-" % support.T2
        t0 = int(os.path.basename(files[0])[len(pref):-len(".json")])
        with open(files[0]) as f:
            return json.load(f), t0


# ----------------------------------------------------------------- E1

class TestE2ECotTurn(_E2EBase):
    """E1 — the bug's turn: two reasonings, each in ITS message with
    ITS buttons, and the close WITHOUT a global index (F46+F48c+F49 together)."""

    def test_each_pass_its_message_its_buttons_and_clean_close(self):
        runtime = self._run_turn(BATCHES_2PASSES)
        chat = self.chat

        # --- the runtime's final answer reached the chat
        self.assertTrue(any("final reply of the turn" in s[2]
                            for s in chat.sends()))

        # --- r1 is born: ITS message, ITS single button
        born1 = [s for s in chat.sends()
                 if chat.buttons(s) == ["CoT 1.1"]]
        self.assertEqual(len(born1), 1)
        ph1 = born1[0][1]
        self.assertIn("1 box", born1[0][2])

        # --- r1 closes: the LAST edit of ph1 with ITS two buttons
        edits1 = [e for e in chat.edits() if e[1] == ph1]
        self.assertTrue(edits1)
        self.assertIn("pass 1", edits1[-1][2])
        self.assertEqual(chat.buttons(edits1[-1]), ["CoT 1.1", "CoT 1.2"])

        # --- r2 is born: ANOTHER message, ONLY its button (the regression:
        #     the bug piled ALL the buttons into the last box)
        born2 = [s for s in chat.sends() if chat.buttons(s) == ["CoT 2.1"]]
        self.assertEqual(len(born2), 1)
        ph2 = born2[0][1]
        self.assertNotEqual(ph2, ph1)
        edits2 = [e for e in chat.edits() if e[1] == ph2]
        self.assertIn("pass 2", edits2[-1][2])
        self.assertEqual(chat.buttons(edits2[-1]), ["CoT 2.1"])

        # --- NO chat message mixes buttons from two reasonings
        #     (F46/F48b's global index is truly gone)
        for e in chat.log:
            b = chat.buttons(e)
            self.assertFalse(
                "CoT 1.1" in b and "CoT 2.1" in b,
                "piling up in %r: %r" % (e[0], b))
        # --- and the index's final_text didn't come back either
        self.assertFalse(any("Reasoning of «" in e[2] for e in chat.log))

        # --- the cache ended up finalized with the message chronology
        d, _t0 = self._cache()
        self.assertIs(d.get("vivo"), None)
        self.assertTrue(d.get("finalizado"))
        self.assertEqual(d.get("rotulos"),
                         ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(d.get("phs"), [ph1, ph1, ph2])

        # --- the F49 feedback loop: the pwlog reconstructs the keyboard
        #     each message ended up showing
        pw = self._pwlog()
        self.assertIn("cot kb phone born r1 ph=%d [1]: CoT 1.1" % ph1, pw)
        self.assertIn("cot kb phone close r1 ph=%d [2]: CoT 1.1 CoT 1.2"
                      % ph1, pw)
        self.assertIn("cot kb phone born r2 ph=%d [1]: CoT 2.1" % ph2, pw)
        self.assertIn("cot kb phone close r2 ph=%d [1]: CoT 2.1" % ph2, pw)
        self.assertRegex(pw, r"live fin: .*cot_msgs=2 .*boxes=3")

        # --- the worker cleaned up EVERYTHING at the end
        self.assertIsNone(self.zt._workers().get(support.T2))
        self.assertTrue(runtime.closed)


# ----------------------------------------------------------------- E2

class TestE2ETapsOnRealCache(_E2EBase):
    """E2 — taps fire /cotb against the cache a REAL turn left behind
    (not a hand-written json): the viewer edits the message where the box
    LIVES, with THAT message's buttons (F49)."""

    def test_the_tap_edits_the_right_message_with_its_buttons(self):
        self._run_turn(BATCHES_2PASSES)
        d, t0 = self._cache()
        ph1, ph2 = d["phs"][0], d["phs"][2]
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                   owner_user_id=9, enabled=True)
        before = len(self.chat.log)

        def tap(n):
            self.zt.process_update_sync(cfg, {
                "chat": {"id": 1}, "from": {"id": 9},
                "text": "/cotb %s %d %d" % (support.T2, t0, n),
                "_cb_data": "ct:%s:%d:%d" % (support.T2, t0, n)})

        # tap box 1 (lives in ph1): edits ph1 with 1.1 and 1.2 — no 2.1
        tap(1)
        new = self.chat.log[before:]
        self.assertEqual([e[0] for e in new], ["edit"])
        self.assertEqual(new[0][1], ph1)
        b = self.chat.buttons(new[0])
        self.assertIn("CoT 1.1", b)
        self.assertIn("CoT 1.2", b)
        self.assertNotIn("CoT 2.1", b)     # the F48b regression
        self.assertIn("x" * 100, new[0][2])   # the CONTENT of box 1
        before = len(self.chat.log)

        # tap box 3 (lives in ph2): edits ph2 with ONLY 2.1
        tap(3)
        new = self.chat.log[before:]
        self.assertEqual([e[0] for e in new], ["edit"])
        self.assertEqual(new[0][1], ph2)
        b = self.chat.buttons(new[0])
        self.assertIn("CoT 2.1", b)
        self.assertNotIn("CoT 1.1", b)
        self.assertIn("z" * 100, new[0][2])

        # nothing new below, no banner — and the pwlog logged both
        pw = self._pwlog()
        self.assertIn("cot kb cotb n=1 ph=%d" % ph1, pw)
        self.assertIn("cot kb cotb n=3 ph=%d" % ph2, pw)


# ----------------------------------------------------------------- E3

class TestE2ESinCoT(_E2EBase):
    """E3 — the classic flow didn't break: no reasoning means no boxes
    or index, the answer still arrives (and F45 says so in one line)."""

    TEXT_BATCHES = [[("text_delta", "hello ")],
                    [("text_delta", "world")]]

    def test_without_buttons_the_reply_arrives_and_the_live_closes(self):
        self._run_turn(self.TEXT_BATCHES, cot_botones=False)
        chat = self.chat
        # without CoT NO message with box buttons is born
        self.assertFalse(any(chat.buttons(s) for s in chat.sends()))
        # the final answer did arrive
        self.assertTrue(any("final reply of the turn" in s[2]
                            for s in chat.sends()))
        # the live box closed with ✅
        self.assertTrue(any(e[0] == "edit" and "✅" in e[2]
                            for e in chat.edits()))
        self.assertRegex(self._pwlog(), r"live fin: .*boxes=0")

    def test_with_buttons_a_turn_without_reasoning_says_so(self):
        # F45: without the notice, zero boxes was indistinguishable from a bug
        self._run_turn(self.TEXT_BATCHES, cot_botones=True)
        notice = [s for s in self.chat.sends() if "no CoT" in s[2]]
        self.assertEqual(len(notice), 1)
        self.assertIn("«CV»", notice[0][2])

    def test_stream_mode_the_classic_text_cot_lives_on(self):
        # cot_stream without cot_botones (the pre-F42 mode): the CoT
        # arrives as permanent TEXT messages «CoT n», the phase notice is
        # ONE, and no buttons — this very e2e uncovered it when the cfg's
        # default got accidentally turned off: it worked the same as ever.
        self._run_turn(BATCHES_2PASSES, cot_botones=False)
        chat = self.chat
        cot_msg = [s for s in chat.sends() if "CoT 1" in s[2]]
        self.assertTrue(cot_msg)
        self.assertTrue(any("CoT 2" in s[2] for s in chat.sends()))
        self.assertFalse(any(chat.buttons(s) for s in chat.sends()))
        phase = [s for s in chat.sends() if "analysis done" in s[2]]
        self.assertEqual(len(phase), 1)      # ONE phase notice per turn
        self.assertTrue(any("final reply of the turn" in s[2]
                            for s in chat.sends()))


# ----------------------------------------------------------------- E4

class TestE2ECommands(_E2EBase):
    """E4 — day-to-day commands through the gateway with REAL dispatch:
    each answers something sensible, nothing blows up, the unknown one warns."""

    def setUp(self):
        super().setUp()
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        owner_user_id=9, enabled=True)

    def _update(self, text, cb=None):
        m = {"chat": {"id": 1}, "from": {"id": 9}, "text": text}
        if cb:
            m["_cb_data"] = cb
        self.zt.process_update_sync(self.cfg, m)

    def test_the_day_to_day_battery(self):
        cases = [
            ("/help", ["status", "task"]),        # the whole menu
            ("/status", None),
            ("/task", ["testing"]),               # the fixture's list
            ("/all_tasks", ["CV"]),
            ("/model", None),
            ("/ping", ["pong"]),
            ("/last", ["Sample last reply"]),
            ("/cotb", ["Usage"]),                # no args: how to use it
        ]
        for text, expected in cases:
            n = len(self.chat.log)
            self._update(text)
            new = [e for e in self.chat.log[n:] if e[0] == "send"]
            self.assertTrue(new, "no reply for %s" % text)
            body = "\n".join(e[2] for e in new)
            for exp in expected or []:
                self.assertIn(exp, body, "%s → %r" % (text, body))

        # the truly nonexistent one still warns (F48a intact)
        self._update("/noexiste")
        self.assertTrue(any("Unknown command" in e[2]
                            for e in self.chat.sends()))

        # and an expired question button injects nothing
        self._update("si", cb="yes")
        self.assertTrue(any("has expired" in e[2] for e in self.chat.sends()))

        # the WHOLE chat with no internal errors
        self.assertFalse(any("Error interno" in e[2] for e in self.chat.log))


# ----------------------------------------------------------------- E5

class TestE2ELateBoxes(_E2EBase):
    """E5 — the 2nd reasoning's send fails (no message_id): its box isn't
    lost — ONE late-comers message is born at close with THAT box, and
    the 1st reasoning stays intact with its buttons (F49)."""

    def test_the_box_without_message_goes_late_and_contaminates_nothing(self):
        self.chat.mark_results = [True, None]   # r2's birth send fails
        self._run_turn(BATCHES_2PASSES)
        chat = self.chat

        # the 1st reasoning alive as ever: ITS message with ITS two buttons
        born1 = [s for s in chat.sends()
                 if chat.buttons(s) == ["CoT 1.1"]]
        self.assertEqual(len(born1), 1)
        ph1 = born1[0][1]
        edits1 = [e for e in chat.edits() if e[1] == ph1]
        self.assertEqual(chat.buttons(edits1[-1]), ["CoT 1.1", "CoT 1.2"])

        # ONE late-comers message with ONLY the orphan box
        lates = [s for s in chat.sends()
                 if "as the turn closed" in s[2]]
        self.assertEqual(len(lates), 1)
        self.assertEqual(chat.buttons(lates[0]), ["CoT 2.1"])
        ph_t = lates[0][1]
        self.assertNotEqual(ph_t, ph1)

        # the cache: box 3 lives in the late-comers message
        d, _t0 = self._cache()
        self.assertEqual(d.get("phs"), [ph1, ph1, ph_t])

        # the pwlog says so, and no message mixes reasonings
        self.assertIn("cot kb phone late ph=%d [1]: CoT 2.1" % ph_t,
                      self._pwlog())
        self.assertRegex(self._pwlog(), r"live fin: .*cot_msgs=2 .*boxes=3")
        for e in chat.log:
            b = chat.buttons(e)
            self.assertFalse("CoT 1.1" in b and "CoT 2.1" in b)


if __name__ == "__main__":
    unittest.main()
