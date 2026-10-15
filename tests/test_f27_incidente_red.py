"""Incidente 14/10 madrugada — "no responde /quota, no se ven boxes, los
cuestionarios se repiten":

1. El poller estaba SANO (offset latiendo, outbox vacío, Telegram 0
   pendientes) — el problema era la red intermitente. PERO el incidente
   destapó tres bugs reales:
2. save_offset ANTES del dispatch: el nuclear real de las 02:03 pudo
   matar al poller entre la confirmación y el envío → batch perdido para
   siempre. Ahora: confirmación POST-dispatch (al-least-once).
3. Cuestionarios AskUserQuestion re-disparados por el CLI re-mostraban
   TODO en vez de reenviar la respuesta (F26 solo cubría permisos).
4. Mensajes del owner desde otro chat: tragados sin rastro.
"""
import os
import sys
import time
import unittest

import support
from zcode_bot import hooks as zc_hooks          # dueño de on_* (F4j)
from zcode_bot.utils import http as zc_http      # seam transporte

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class TestReplayAskUserQuestion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.enviados = []
        support.parchea(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.enviados.append(text), True)[1])
        self.emit = []
        support.parchea(self, zc_hooks, "emit", self.emit.append)

    def _pregunta(self, q="¿Plan A o B?", sid=None):
        return {"session_id": sid or support.T1,
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{
                    "question": q,
                    "options": [{"label": "A"}, {"label": "B"}]}]}}

    def _responder(self):
        # flujo normal (phone): muestra botones, usuario elige opción 2
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: ("text", "2"))
        self.zt.on_ask_user_question(
            _cfg(self.zt, question_mode="phone"), self._pregunta())

    async def test_redisparo_no_remuestra_el_cuestionario(self):
        zt = self.zt
        self._responder()
        n_antes = len(self.enviados)
        # el CLI re-dispara la MISMA pregunta → sin botones ni espera
        def prohibido(cfg, hint):
            raise AssertionError("no debe volver a esperar respuesta")
        support.parchea(self, zc_hooks, "wait_reply", prohibido)
        zt.on_ask_user_question(_cfg(zt), self._pregunta())
        nuevos = self.enviados[n_antes:]
        self.assertTrue(any("reenviando" in t.lower() for t in nuevos))
        # emite la MISMA respuesta sin intervención
        d = self.emit[-1]["hookSpecificOutput"]
        self.assertEqual(d["permissionDecisionReason"],
                         "El usuario respondió por Telegram: opción 2")

    async def test_pregunta_distinta_si_se_muestra(self):
        zt = self.zt
        self._responder()
        n_antes = len(self.enviados)
        support.parchea(self, zc_hooks, "wait_reply",  # timeout
                        lambda cfg, hint: (None, None))
        zt.on_ask_user_question(_cfg(zt), self._pregunta(q="¿OTRA COSA?"))
        self.assertGreater(len(self.enviados), n_antes)
        self.assertIn("El agente pregunta", self.enviados[-1])

    async def test_replay_vence_a_los_10_min(self):
        zt = self.zt
        self._responder()
        key = zt._perm_key(support.T1, "AskUserQuestion",
                           self._pregunta()["tool_input"])

        def fn(d):
            d[key]["ts"] = time.time() - 700  # 11.6 min
            return d
        zt._store("bot-perm.json").update_cross_process(fn, {})
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        zt.on_ask_user_question(_cfg(zt, question_mode="phone"),
                                self._pregunta())
        # vencida: re-muestra el cuestionario (y no hay "reenviando")
        self.assertTrue(any("El agente pregunta" in t for t in self.enviados))
        self.assertFalse(any("reenviando" in t.lower() for t in self.enviados))


class TestRedisparoPreguntaWorker(unittest.IsolatedAsyncioTestCase):
    """El handler de preguntas del WORKER (interaction/requestUserInput)
    era el que re-mostraba el cuestionario: el CLI re-dispara y el handler
    re-creaba la q y re-enviaba los botones. Tres capas de defensa:
    replay (<10 min) → gemela pendiente (_q_gemela) → nueva. Todo
    SÍNCRONO: QUESTIONS.wait se stubbea — la coordinación real por
    archivos (kqueue, productores/consumidores) la ejercita
    test_concurrency.TestQuestionFiles."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.enviados = []
        # F4g: _user_input_handler vive en workers y usa uhttp.send
        from unittest import mock
        from zcode_bot.utils import http as zc_http
        ps = mock.patch.object(
            zc_http, "send",
            lambda cfg, text, loud=None, markup=None:
                (self.enviados.append(text), True)[1])
        ps.start()
        self.addCleanup(ps.stop)
        self.zt.set_active_bot_session(
            {"sid": support.T1, "title": "testing", "ws": "/w"})
        self.esperas = []
        self.respuesta = None  # lo que "devuelve el canal" (o None: timeout)

        def wait_stub(rid, timeout=None):
            self.esperas.append(rid)
            return self.respuesta
        self.zt._questions().wait = wait_stub

    def _preguntas(self):
        return {"questions": [{"header": "Alcance", "multiSelect": False,
                               "options": [
                                   {"label": "Moderado (Recomendado)"},
                                   {"label": "Agresivo (todo)"},
                                   {"label": "Light"}]}]}

    async def test_redisparo_despues_de_responder_devuelve_directo(self):
        zt = self.zt
        h = zt._user_input_handler({}, support.T1, "Alcance")
        self.respuesta = "Moderado (Recomendado)"
        r1 = h(self._preguntas())  # muestra, "recibe" y recuerda (replay)
        self.assertEqual(r1["content"]["answer"], "Moderado (Recomendado)")
        n_mensajes = len(self.enviados)
        # el CLI re-dispara LA MISMA pregunta → respuesta directa por
        # replay, sin re-mostrar y sin nueva espera
        r2 = h(self._preguntas())
        self.assertEqual(r2, r1)
        self.assertEqual(len(self.enviados), n_mensajes)  # ni un mensaje más
        self.assertEqual(len(self.esperas), 1)            # ni una espera más

    async def test_redisparo_antes_de_responder_no_duplica(self):
        zt = self.zt
        h = zt._user_input_handler({}, support.T1, "Alcance")
        # la MISMA pregunta ya está pendiente en el canal (primer disparo
        # en curso): el re-disparo debe reusar SU rid sin re-mostrar
        zt._questions().ask("rid-en-curso", self._preguntas()["questions"],
                         "Alcance", support.T1)
        self.respuesta = "Agresivo (todo)"
        r2 = h(self._preguntas())
        self.assertEqual(self.esperas, ["rid-en-curso"])  # espera la EXISTENTE
        self.assertEqual(self.enviados, [])   # cero preguntas nuevas al chat
        self.assertEqual(r2["content"]["answer"], "Agresivo (todo)")
        self.assertEqual(len(zt._questions().pendings()), 1)  # no duplicó canal

    async def test_pregunta_distinta_si_se_muestra(self):
        zt = self.zt
        h = zt._user_input_handler({}, support.T1, "Alcance")
        otras = {"questions": [{"header": "Otra", "options": [
            {"label": "X"}, {"label": "Y"}]}]}
        self.respuesta = None  # nadie responde: TTL vencido → decline
        r = h(otras)
        self.assertTrue(any("❓" in t for t in self.enviados))
        self.assertEqual(r["action"], "decline")


if __name__ == "__main__":
    unittest.main()
