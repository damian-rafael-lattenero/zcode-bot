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

    def test_round_trip_persistido(self):
        d = {"sid": "sess_1", "title": "bot", "ws": "/w",
             "model": {"modelId": "GLM-5.3"}, "mode": "yolo"}
        self.zt.set_active_bot_session(d)
        ses = self.zt.active_bot_session()
        self.assertEqual(ses.to_dict(), d)  # the JSON shape does not change

    def test_from_dict_tolerante_a_shape_viejo(self):
        # old file without model/mode + unknown fields from the future
        ses = self.zt.BotSession.from_dict(
            {"sid": "sess_2", "title": "cv", "nuevo_campo": "x"})
        self.assertEqual((ses.sid, ses.title), ("sess_2", "cv"))
        self.assertIsNone(ses.model)
        self.assertEqual(ses.to_dict(),
                         {"sid": "sess_2", "title": "cv", "ws": ""})

    def test_sesion_vacia(self):
        ses = self.zt.BotSession.from_dict(None)
        self.assertEqual(ses.get("sid"), "")
        self.assertFalse(ses.get("sid"))  # the `act and act.get(sid)` checks keep working

    def test_compat_mapping_para_consumidores_viejos(self):
        ses = self.zt.BotSession(sid="s", title="t", ws="w", mode="yolo")
        self.assertEqual(ses["mode"], "yolo")          # __getitem__
        self.assertEqual(ses.get("title"), "t")         # .get
        # dict(sesion, ...) — pattern of _cmd_mode/_set_sel
        self.assertEqual(dict(ses, mode="plan")["mode"], "plan")
        # None values do NOT go into the dict (historical JSON shape)
        self.assertNotIn("model", dict(ses, mode="plan"))

    def test_set_accepta_la_propia_entidad(self):
        ses = self.zt.BotSession(sid="s3", title="x", ws="/w")
        self.zt.set_active_bot_session(ses)
        self.assertEqual(self.zt.active_bot_session().sid, "s3")


class TestWaitReply(unittest.TestCase):
    def test_es_tupla_historica(self):
        # test stubs and the `kind, text = wait_reply(...)` unpackings
        # stay intact: the NamedTuple IS the (kind, text) tuple
        wr = support.fresh(self).WaitReply("yes", "dale")
        kind, text = wr
        self.assertEqual((kind, text), ("yes", "dale"))
        self.assertEqual(tuple(wr), ("yes", "dale"))
        self.assertEqual(support.fresh(self).WaitReply(None, None).kind, None)


class TestToolStep(unittest.TestCase):
    def test_es_tupla_historica(self):
        from zcode_bot.messages import ToolStep, _steps_of_messages
        msgs = [{"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "tool": "Bash",
             "state": {"input": {"command": ["ls", "-la"]}}}]}]
        pasos = _steps_of_messages(msgs)
        nombre, desc = pasos[0]  # historical unpacking
        self.assertEqual((nombre, desc), ("Bash", "ls -la"))
        self.assertEqual(pasos[0], ToolStep("Bash", "ls -la"))


if __name__ == "__main__":
    unittest.main()


class TestNuclearReport(unittest.TestCase):
    def test_round_trip_del_contrato_body_watchdog(self):
        # the body writes the report BEFORE the kickstart that kills it;
        # the detached watchdog reads it after — JSON is the only contract
        from zcode_bot.nuclear import NuclearReport
        r = NuclearReport(old_pid=111, t0=1.5, dur=42, frenadas=2, totales=3,
                          mac=1, cola=4, vivos=0, app_ok=True,
                          notes=["nota x"])
        r2 = NuclearReport.from_dict(r.to_dict())
        self.assertEqual(r2, r)
        self.assertEqual(r2.to_dict(), r.to_dict())

    def test_from_dict_tolerante(self):
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

    def test_frontera_sqlite_devuelve_tareas_tipadas(self):
        from zcode_bot.reporting import Task, _ctx_tasks
        tasks = _ctx_tasks(10)
        self.assertTrue(tasks)
        self.assertIsInstance(tasks[0], Task)
        # compat mapping: old consumers index it like a dict
        self.assertEqual(tasks[0]["sid"], tasks[0].sid)
        self.assertEqual(tasks[0].get("title"), tasks[0].title)

    def test_from_dict_tolerante(self):
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

    def test_cancelled_libera_como_failed(self):
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
