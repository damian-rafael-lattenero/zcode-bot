"""Plan approval loop (13/10): Damian approved the plan from his phone
and the bot asked him to approve OVER AND OVER.

Causes fixed:
1. The message "⏱ No reply over Telegram: the dialog stayed in
   the app" was ALWAYS sent — even after a successful "👍 Approved".
   The user believed it hadn't worked and approved again.
2. The CLI sometimes re-fires the same PermissionRequest: with no
   memory of the decision, the bot re-asked from scratch. There is
   replay now.
3. Plan approval without a feedback option: ✏️ Add something to the plan.
"""
import os
import sys
import unittest
from unittest import mock

import support
from zcode_bot import hooks as zc_hooks          # owner of on_* (F4j)
from zcode_bot.utils import http as zc_http      # transport seam

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


def _payload(sid=support.T1, tool="ExitPlanMode", inp=None):
    return {"session_id": sid, "tool_name": tool,
            "tool_input": inp if inp is not None else {"plan": "do X"}}


class _EmitSpy:
    def __init__(self, zt):
        self.outputs = []
        self.zt = zt

    def __call__(self, obj):
        self.outputs.append(obj)


class TestPermissionFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sent = []
        support.patch(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sent.append((text, markup)), True)[1])
        self.emit = _EmitSpy(self.zt)
        support.patch(self, zc_hooks, "emit", self.emit)

    def _approve(self):
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: ("yes", "si"))  # noqa: spanish — bilingual classify input, permanent
        self.zt.on_permission_request(_cfg(self.zt), _payload())

    async def test_approved_does_NOT_say_no_reply(self):
        # THE loop regression: after "👍 Aprobado" there must NOT follow
        # "⏱ No reply ... the dialog stayed in the app"
        self._approve()
        texts = [t for t, _ in self.sent]
        self.assertTrue(any("Approved" in t for t in texts))
        self.assertFalse(any("No reply" in t for t in texts))

    async def test_denied_does_NOT_say_no_reply(self):
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: ("no", "no"))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        texts = [t for t, _ in self.sent]
        self.assertFalse(any("No reply" in t for t in texts))

    async def test_timeout_does_say_no_reply(self):
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        self.assertTrue(any("No reply over Telegram" in t for t, _ in self.sent))
        self.assertEqual(self.emit.outputs, [])  # no decision → no emit

    async def test_approval_emits_allow(self):
        self._approve()
        d = self.emit.outputs[0]["hookSpecificOutput"]
        self.assertEqual(d["decision"]["behavior"], "allow")

    async def test_replay_same_fingerprint_does_not_requestion(self):
        # the CLI re-fires the same request → same decision, no new 🔐
        self._approve()
        n_before = len(self.sent)
        support.patch(
            self, zc_hooks, "wait_reply",
            lambda cfg, hint: (_ for _ in ()).throw(
                AssertionError("must NOT go back to waiting for a reply")))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        texts = [t for t, _ in self.sent[n_before:]]
        self.assertTrue(any("resending" in t.lower() for t in texts))
        self.assertFalse(any("Approval needed" in t for t in texts))
        self.assertEqual(self.emit.outputs[1]["hookSpecificOutput"]
                         ["decision"]["behavior"], "allow")

    async def test_different_fingerprint_does_question(self):
        self._approve()
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        other = _payload(inp={"plan": "do SOMETHING ELSE"})
        self.zt.on_permission_request(_cfg(self.zt), other)
        self.assertTrue(any("No reply over Telegram" in t for t, _ in self.sent))


class TestPlanApproval(unittest.IsolatedAsyncioTestCase):
    """🗺 ExitPlanMode: Approve / Reject / ✏️ Add-something buttons."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sent = []
        support.patch(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sent.append((text, markup)), True)[1])
        self.emit = _EmitSpy(self.zt)
        support.patch(self, zc_hooks, "emit", self.emit)

    async def test_plan_buttons(self):
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        text, markup = self.sent[0]
        self.assertIn("Plan ready for approval", text)
        buttons = [b for row in markup["inline_keyboard"] for b in row]
        datas = [b["callback_data"] for b in buttons]
        self.assertIn("yes", datas)
        self.assertIn("no", datas)
        self.assertIn("planfix", datas)  # ✏️ add something to the plan
        labels = " ".join(b["text"] for b in buttons)
        self.assertIn("Add something to the plan", labels)

    async def test_planfix_denies_with_feedback(self):
        replies = [("text", "planfix"), ("text", "change phase 2")]
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: replies.pop(0))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        d = self.emit.outputs[0]["hookSpecificOutput"]
        self.assertEqual(d["decision"]["behavior"], "deny")
        self.assertIn("change phase 2", d["decision"]["message"])
        self.assertTrue(any("Changes sent" in t for t, _ in self.sent))

    async def test_planfix_without_text_stays_pending(self):
        replies = [("text", "planfix"), ("/no", "/no")]
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: replies.pop(0))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        self.assertEqual(self.emit.outputs, [])
        self.assertTrue(any("pending" in t for t, _ in self.sent))

    async def test_normal_permission_shows_no_plan_buttons(self):
        support.patch(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        self.zt.on_permission_request(_cfg(self.zt),
                                      _payload(tool="Bash", inp={"command": "ls"}))
        text, markup = self.sent[0]
        self.assertIn("Approval needed", text)
        self.assertNotIn("Plan ready", text)
        datas = [b["callback_data"] for row in markup["inline_keyboard"]
                 for b in row]
        self.assertNotIn("planfix", datas)


if __name__ == "__main__":
    unittest.main()
