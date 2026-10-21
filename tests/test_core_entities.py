"""Core entities (F4b): BotSession and WaitReply.

BotSession: JSON round-trip + tolerance to old shapes (PRODUCTION state
files must remain readable) + compat mapping for non-migrated consumers.
WaitReply: NamedTuple drop-in for the historical (kind, text) tuple.
"""
import unittest

import support


class TestBotSession(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_persisted_round_trip(self):
        d = {"sid": "sess_1", "title": "bot", "ws": "/w",
             "model": {"modelId": "GLM-5.3"}, "mode": "yolo"}
        self.zt.set_active_bot_session(d)
        session = self.zt.active_bot_session()
        self.assertEqual(session.to_dict(), d)  # the JSON shape does not change

    def test_from_dict_tolerant_to_old_shape(self):
        # old file without model/mode + unknown fields from the future
        session = self.zt.BotSession.from_dict(
            {"sid": "sess_2", "title": "cv", "nuevo_campo": "x"})
        self.assertEqual((session.sid, session.title), ("sess_2", "cv"))
        self.assertIsNone(session.model)
        self.assertEqual(session.to_dict(),
                         {"sid": "sess_2", "title": "cv", "ws": ""})

    def test_empty_session(self):
        session = self.zt.BotSession.from_dict(None)
        self.assertEqual(session.get("sid"), "")
        self.assertFalse(session.get("sid"))  # the `act and act.get(sid)` checks keep working

    def test_compat_mapping_for_old_consumers(self):
        session = self.zt.BotSession(sid="s", title="t", ws="w", mode="yolo")
        self.assertEqual(session["mode"], "yolo")      # __getitem__
        self.assertEqual(session.get("title"), "t")    # .get
        # dict(sesion, ...) — pattern of _cmd_mode/_set_sel
        self.assertEqual(dict(session, mode="plan")["mode"], "plan")
        # None values do NOT go into the dict (historical JSON shape)
        self.assertNotIn("model", dict(session, mode="plan"))

    def test_set_accepts_the_entity_itself(self):
        session = self.zt.BotSession(sid="s3", title="x", ws="/w")
        self.zt.set_active_bot_session(session)
        self.assertEqual(self.zt.active_bot_session().sid, "s3")


class TestWaitReply(unittest.TestCase):
    def test_is_historical_tuple(self):
        # test stubs and the `kind, text = wait_reply(...)` unpackings
        # stay intact: the NamedTuple IS the (kind, text) tuple
        wr = support.fresh(self).WaitReply("yes", "dale")
        kind, text = wr
        self.assertEqual((kind, text), ("yes", "dale"))
        self.assertEqual(tuple(wr), ("yes", "dale"))
        self.assertEqual(support.fresh(self).WaitReply(None, None).kind, None)


class TestToolStep(unittest.TestCase):
    def test_is_historical_tuple(self):
        from zcode_bot.messages import ToolStep, _steps_of_messages
        msgs = [{"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "tool": "Bash",
             "state": {"input": {"command": ["ls", "-la"]}}}]}]
        steps = _steps_of_messages(msgs)
        name, desc = steps[0]  # historical unpacking
        self.assertEqual((name, desc), ("Bash", "ls -la"))
        self.assertEqual(steps[0], ToolStep("Bash", "ls -la"))


if __name__ == "__main__":
    unittest.main()


class TestNuclearReport(unittest.TestCase):
    def test_round_trip_of_the_body_watchdog_contract(self):
        # the body writes the report BEFORE the kickstart that kills it;
        # the detached watchdog reads it after — JSON is the only contract
        from zcode_bot.nuclear import NuclearReport
        r = NuclearReport(old_pid=111, t0=1.5, dur=42, stopped=2, total=3,
                          mac=1, queued=4, alive=0, app_ok=True,
                          notes=["nota x"])
        r2 = NuclearReport.from_dict(r.to_dict())
        self.assertEqual(r2, r)
        self.assertEqual(r2.to_dict(), r.to_dict())

    def test_from_dict_tolerant(self):
        from zcode_bot.nuclear import NuclearReport
        # old/corrupt report: sane defaults, the watchdog never blows up
        r = NuclearReport.from_dict({"old_pid": 5, "notes": "no-lista"})
        self.assertEqual(r.old_pid, 5)
        self.assertEqual(r.notes, [])
        self.assertFalse(r.app_ok)
        self.assertEqual(NuclearReport.from_dict(None).dur, 0)


class TestTask(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_sqlite_boundary_returns_typed_tasks(self):
        from zcode_bot.reporting import Task, _ctx_tasks
        tasks = _ctx_tasks(10)
        self.assertTrue(tasks)
        self.assertIsInstance(tasks[0], Task)
        # compat mapping: old consumers index it like a dict
        self.assertEqual(tasks[0]["sid"], tasks[0].sid)
        self.assertEqual(tasks[0].get("title"), tasks[0].title)

    def test_from_dict_tolerant(self):
        from zcode_bot.reporting import Task
        t = Task.from_dict({"sid": "s1", "ctx": "1200", "basura": 1})
        self.assertEqual((t.sid, t.ctx, t.n), ("s1", 1200, 0))
        t2 = Task.from_dict(None)
        self.assertEqual(t2.title, "")


class TestRunningTurnsCancelled(unittest.TestCase):
    """F51b — turn.cancelled also releases the session. Today the app
    cancels as turn.failed with error «Turn was cancelled» (seen 20/10
    14:55), but if it someday emits turn.cancelled and the replay doesn't
    know it, the session stayed «busy» for 2 h: phone messages queued for
    no reason and the drain skipping it forever."""

    def test_cancelled_releases_like_failed(self):
        from unittest import mock
        from zcode_bot import core as zc_core
        evs = [(100.0, "sess_a", "turn.started", {}),
               (110.0, "sess_a", "turn.cancelled", {}),
               (100.0, "sess_b", "turn.started", {}),
               (110.0, "sess_b", "turn.failed", {}),
               (100.0, "sess_c", "turn.started", {}),
               (110.0, "sess_c", "turn.phase.completed", {})]
        with mock.patch.object(zc_core, "zc_logs") as logs:
            logs.iter_events.return_value = evs
            self.assertEqual(zc_core.running_turns(), {"sess_c": 100.0})
