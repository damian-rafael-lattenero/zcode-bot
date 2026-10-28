"""Incident in the early hours of 14/10 — "/quota doesn't answer, no
boxes visible, questionnaires repeat":

1. The poller was HEALTHY (offset beating, outbox empty, Telegram 0
   pending) — the problem was the flaky network. BUT the incident
   unearthed three real bugs:
2. save-offset BEFORE dispatch: the real nuclear at 02:03 could have
   killed the poller between the confirmation and the send → batch
   lost forever. Now: POST-dispatch confirmation (at-least-once).
3. AskUserQuestion questionnaires re-fired by the CLI re-showed
   EVERYTHING instead of replaying the answer (F26 only covered
   permissions).
4. Owner messages from another chat: swallowed without a trace.
"""
import os
import time
import unittest

import support
from zcode_bot import hooks as zc_hooks          # owner of on_* (F4j)
from zcode_bot.utils import http as zc_http      # transport seam

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class TestReplayAskUserQuestion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sent = []
        support.patch(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None, a_outbox=True:
                        (self.sent.append(text), True)[1])
        self.emit = []
        support.patch(self, zc_hooks, "emit", self.emit.append)

    def _question(self, q="Plan A or B?", sid=None):
        return {"session_id": sid or support.T1,
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{
                    "question": q,
                    "options": [{"label": "A"}, {"label": "B"}]}]}}

    def _responder(self):
        # normal flow (phone): shows buttons, user picks option 2
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: ("text", "2"))
        self.zt.on_ask_user_question(
            _cfg(self.zt, question_mode="phone"), self._question())

    async def test_redispatch_does_not_reshow_the_questionnaire(self):
        zt = self.zt
        self._responder()
        n_before = len(self.sent)
        # the CLI re-fires the SAME question → no buttons, no waiting
        def prohibido(cfg, hint):
            raise AssertionError("must not go back to wait_until for the reply")
        support.patch(self, zc_hooks, "wait_reply", prohibido)
        zt.on_ask_user_question(_cfg(zt), self._question())
        new_ones = self.sent[n_before:]
        self.assertTrue(any("resending" in t.lower() for t in new_ones))
        # emits the SAME answer without intervention
        d = self.emit[-1]["hookSpecificOutput"]
        self.assertEqual(d["permissionDecisionReason"],
                         "The user answered from Telegram: option 2")

    async def test_different_question_is_shown(self):
        zt = self.zt
        self._responder()
        n_before = len(self.sent)
        support.patch(self, zc_hooks, "wait_reply",  # timeout
                        lambda cfg, hint: (None, None))
        zt.on_ask_user_question(_cfg(zt), self._question(q="SOMETHING ELSE?"))
        self.assertGreater(len(self.sent), n_before)
        self.assertIn("The agent asks", self.sent[-1])

    async def test_replay_expires_after_10_min(self):
        zt = self.zt
        self._responder()
        key = zt._perm_key(support.T1, "AskUserQuestion",
                           self._question()["tool_input"])

        def fn(d):
            d[key]["ts"] = time.time() - 700  # 11.6 min
            return d
        zt._store("bot-perm.json").update_cross_process(fn, {})
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        zt.on_ask_user_question(_cfg(zt, question_mode="phone"),
                                self._question())
        # expired: re-shows the questionnaire (and there is no "reenviando")
        self.assertTrue(any("The agent asks" in t for t in self.sent))
        self.assertFalse(any("resending" in t.lower() for t in self.sent))


class TestWorkerQuestionRedispatch(unittest.IsolatedAsyncioTestCase):
    """The WORKER's question handler (interaction/requestUserInput) was
    the one re-showing the questionnaire: the CLI re-fires and the
    handler re-created the q and re-sent the buttons. Three layers of
    defense: replay (<10 min) → pending twin (_twin_q) → new.
    Everything is SYNCHRONOUS: QUESTIONS.wait is stubbed — the real
    file-based coordination (kqueue, producers/consumers) is exercised
    by test_concurrency.TestQuestionFiles."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sent = []
        # F4g: _user_input_handler lives in workers and uses uhttp.send
        from unittest import mock
        from zcode_bot.utils import http as zc_http
        ps = mock.patch.object(
            zc_http, "send",
            lambda cfg, text, loud=None, markup=None, a_outbox=True:
                (self.sent.append(text), True)[1])
        ps.start()
        self.addCleanup(ps.stop)
        self.zt.set_active_bot_session(
            {"sid": support.T1, "title": "testing", "ws": "/w"})
        self.esperas = []
        self.reply = None  # what the "channel returns" (or None: timeout)

        def wait_stub(rid, timeout=None):
            self.esperas.append(rid)
            return self.reply
        self.zt._questions().wait = wait_stub

    def _questions_payload(self):
        return {"questions": [{"header": "Alcance", "multiSelect": False,
                               "options": [
                                   {"label": "Moderado (Recomendado)"},
                                   {"label": "Aggressive (all)"},
                                   {"label": "Light"}]}]}

    async def test_redispatch_after_answering_returns_directly(self):
        zt = self.zt
        h = zt._user_input_handler({}, support.T1, "Alcance")
        self.reply = "Moderado (Recomendado)"
        r1 = h(self._questions_payload())  # shows, "receives" and remembers (replay)
        self.assertEqual(r1["content"]["answer"], "Moderado (Recomendado)")
        n_mensajes = len(self.sent)
        # the CLI re-fires THE SAME question → direct answer by
        # replay, without re-showing and without a new wait
        r2 = h(self._questions_payload())
        self.assertEqual(r2, r1)
        self.assertEqual(len(self.sent), n_mensajes)  # not one message more
        self.assertEqual(len(self.esperas), 1)            # not one more wait

    async def test_redispatch_before_answering_does_not_duplicate(self):
        zt = self.zt
        h = zt._user_input_handler({}, support.T1, "Alcance")
        # the SAME question is already pending in the channel (first
        # dispatch in flight): the re-fire must reuse ITS rid without re-showing
        zt._questions().ask("rid-en-curso", self._questions_payload()["questions"],
                         "Alcance", support.T1)
        self.reply = "Aggressive (all)"
        r2 = h(self._questions_payload())
        self.assertEqual(self.esperas, ["rid-en-curso"])  # waits on the EXISTING one
        # F36: the incident's invariant is NOT re-showing the question —
        # the «📬 reached the agent» ack (acks) IS legitimate: the answer
        # travels to the agent again in this re-fire
        self.assertEqual([s for s in self.sent if "❓" in s], [])
        self.assertTrue(any("answer to the agent" in s
                         for s in self.sent))
        self.assertEqual(r2["content"]["answer"], "Aggressive (all)")
        self.assertEqual(len(zt._questions().pendings()), 1)  # did not duplicate the channel

    async def test_different_question_is_shown(self):
        zt = self.zt
        h = zt._user_input_handler({}, support.T1, "Alcance")
        otras = {"questions": [{"header": "Otra", "options": [
            {"label": "X"}, {"label": "Y"}]}]}
        self.reply = None  # nobody answers: TTL expired → decline
        r = h(otras)
        self.assertTrue(any("❓" in t for t in self.sent))
        self.assertEqual(r["action"], "decline")


if __name__ == "__main__":
    unittest.main()
