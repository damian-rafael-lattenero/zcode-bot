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
