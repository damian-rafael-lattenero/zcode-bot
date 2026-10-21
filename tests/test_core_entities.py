"""Entidades de core (F4b): BotSession y WaitReply.

BotSession: round-trip JSON + tolerancia a shapes viejos (los archivos de
state de PRODUCCIÓN se tienen que poder seguir leyendo) + compat mapping
para los consumidores no migrados. WaitReply: NamedTuple drop-in de la
tupla (kind, text) histórica.
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
        self.assertEqual(ses.to_dict(), d)  # el JSON no cambia de shape

    def test_from_dict_tolerante_a_shape_viejo(self):
        # archivo viejo sin model/mode + campos desconocidos del futuro
        ses = self.zt.BotSession.from_dict(
            {"sid": "sess_2", "title": "cv", "nuevo_campo": "x"})
        self.assertEqual((ses.sid, ses.title), ("sess_2", "cv"))
        self.assertIsNone(ses.model)
        self.assertEqual(ses.to_dict(),
                         {"sid": "sess_2", "title": "cv", "ws": ""})

    def test_sesion_vacia(self):
        ses = self.zt.BotSession.from_dict(None)
        self.assertEqual(ses.get("sid"), "")
        self.assertFalse(ses.get("sid"))  # los `act and act.get(sid)` siguen

    def test_compat_mapping_para_consumidores_viejos(self):
        ses = self.zt.BotSession(sid="s", title="t", ws="w", mode="yolo")
        self.assertEqual(ses["mode"], "yolo")          # __getitem__
        self.assertEqual(ses.get("title"), "t")         # .get
        # dict(sesion, ...) — patrón de _cmd_mode/_set_sel
        self.assertEqual(dict(ses, mode="plan")["mode"], "plan")
        # los None NO van al dict (shape histórico del JSON)
        self.assertNotIn("model", dict(ses, mode="plan"))

    def test_set_accepta_la_propia_entidad(self):
        ses = self.zt.BotSession(sid="s3", title="x", ws="/w")
        self.zt.set_active_bot_session(ses)
        self.assertEqual(self.zt.active_bot_session().sid, "s3")


class TestWaitReply(unittest.TestCase):
    def test_es_tupla_historica(self):
        # los stubs de tests y los unpackings `kind, text = wait_reply(...)`
        # siguen intactos: NamedTuple ES la tupla (kind, text)
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
        nombre, desc = pasos[0]  # unpacking histórico
        self.assertEqual((nombre, desc), ("Bash", "ls -la"))
        self.assertEqual(pasos[0], ToolStep("Bash", "ls -la"))


if __name__ == "__main__":
    unittest.main()


class TestNuclearReport(unittest.TestCase):
    def test_round_trip_del_contrato_body_watchdog(self):
        # el body escribe el reporte ANTES del kickstart que lo mata; el
        # watchdog detached lo lee después — el JSON es el único contrato
        from zcode_bot.nuclear import NuclearReport
        r = NuclearReport(old_pid=111, t0=1.5, dur=42, frenadas=2, totales=3,
                          mac=1, cola=4, vivos=0, app_ok=True,
                          notes=["nota x"])
        r2 = NuclearReport.from_dict(r.to_dict())
        self.assertEqual(r2, r)
        self.assertEqual(r2.to_dict(), r.to_dict())

    def test_from_dict_tolerante(self):
        from zcode_bot.nuclear import NuclearReport
        # reporte viejo/corrupto: defaults sanos, nunca explota el watchdog
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
        # compat mapping: los consumidores viejos indexan como dict
        self.assertEqual(tasks[0]["sid"], tasks[0].sid)
        self.assertEqual(tasks[0].get("title"), tasks[0].title)

    def test_from_dict_tolerante(self):
        from zcode_bot.reporting import Task
        t = Task.from_dict({"sid": "s1", "ctx": "1200", "basura": 1})
        self.assertEqual((t.sid, t.ctx, t.n), ("s1", 1200, 0))
        t2 = Task.from_dict(None)
        self.assertEqual(t2.title, "")


class TestRunningTurnsCancelled(unittest.TestCase):
    """F51b — turn.cancelled también libera la sesión. Hoy la app cancela
    como turn.failed con error «Turn was cancelled» (visto 20/10 14:55),
    pero si algún día emite turn.cancelled y el replay no lo conoce, la
    sesión quedaba «ocupada» 2 h: los mensajes del celu encolados sin
    razón y el drain saltándola para siempre."""

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
