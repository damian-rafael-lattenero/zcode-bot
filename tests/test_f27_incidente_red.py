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
        self.zt.send = lambda cfg, text, loud=None, markup=None: (
            self.enviados.append(text), True)[1]
        self.emit = []
        self.zt.emit = self.emit.append

    def _pregunta(self, q="¿Plan A o B?", sid=None):
        return {"session_id": sid or support.T1,
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{
                    "question": q,
                    "options": [{"label": "A"}, {"label": "B"}]}]}}

    def _responder(self):
        # flujo normal (phone): muestra botones, usuario elige opción 2
        self.zt.wait_reply = lambda cfg, hint: ("text", "2")
        self.zt.on_ask_user_question(
            _cfg(self.zt, question_mode="phone"), self._pregunta())

    async def test_redisparo_no_remuestra_el_cuestionario(self):
        zt = self.zt
        self._responder()
        n_antes = len(self.enviados)
        # el CLI re-dispara la MISMA pregunta → sin botones ni espera
        def prohibido(cfg, hint):
            raise AssertionError("no debe volver a esperar respuesta")
        zt.wait_reply = prohibido
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
        zt.wait_reply = lambda cfg, hint: (None, None)  # timeout
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
        zt.wait_reply = lambda cfg, hint: (None, None)
        zt.on_ask_user_question(_cfg(zt, question_mode="phone"),
                                self._pregunta())
        # vencida: re-muestra el cuestionario (y no hay "reenviando")
        self.assertTrue(any("El agente pregunta" in t for t in self.enviados))
        self.assertFalse(any("reenviando" in t.lower() for t in self.enviados))


class TestOffsetPostDispatch(unittest.IsolatedAsyncioTestCase):
    """El offset se confirma DESPUÉS de procesar el batch: un kill a
    mitad re-entrega (al-least-once) — nunca más batches perdidos."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends = []
        self.zt.send = lambda cfg, text, loud=None, markup=None: (
            self.sends.append(text), True)[1]
        self.zt.react = lambda cfg, msg, e: None
        self.zt.typing = lambda cfg, s=5: None
        self.zt._orphans_check = lambda cfg: None
        self.zt.outbox_flush = lambda cfg: None
        self.zt.busy_fresh = lambda: False
        self.zt.register_bot_menu = lambda cfg: True
        self.zt.load_config = lambda: _cfg(self.zt)
        self.zt.housekeeping = lambda: None

    def _correr_una_vuelta(self, batch, guarda_offset_al_final=True):
        zt = self.zt
        llamadas = {"fetch": 0}

        def fake_fetch(cfg, offset, timeout):
            llamadas["fetch"] += 1
            if llamadas["fetch"] == 1:
                return 4242, batch, len(batch)
            raise KeyboardInterrupt  # cortar el loop tras la 1ª vuelta
        zt.fetch_updates = fake_fetch
        try:
            zt.cmd_watch_commands()
        except KeyboardInterrupt:
            pass
        return llamadas

    async def test_offset_confirmado_despues_del_dispatch(self):
        zt = self.zt
        orden = []
        zt.save_offset = lambda o: orden.append(("save", o))
        zt.handle_command = lambda cfg, text: orden.append(("disp", text)) or "ok"
        batch = [{"update_id": 9, "message_id": 1, "text": "/status",
                  "chat": {"id": 1}, "from": {"id": 9}}]
        self._correr_una_vuelta(batch)
        self.assertEqual(orden, [("disp", "/status"), ("save", 4242)])

    async def test_kill_a_mitad_no_confirma(self):
        # si el dispatch explota de forma sistémica, el batch igual se
        # confirma (el catch lo registra) — pero un KILL del proceso deja
        # el offset SIN confirmar → Telegram re-entrega
        zt = self.zt
        orden = []

        def explota(cfg, text):
            raise RuntimeError("boom sistémico")
        zt.handle_command = explota
        zt.log = lambda *a, **k: orden.append("log")
        batch = [{"update_id": 9, "message_id": 1, "text": "/status",
                  "chat": {"id": 1}, "from": {"id": 9}}]
        try:
            self._correr_una_vuelta(batch)
        except Exception:
            pass
        self.assertTrue(orden)  # el catch corrió (el loop no muere)


class TestFiltroGruposConRastro(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends = []
        self.zt.send = lambda cfg, text, loud=None, markup=None: (
            self.sends.append(text), True)[1]
        self.zt.react = lambda cfg, msg, e: None
        self.zt.typing = lambda cfg, s=5: None
        self.zt._orphans_check = lambda cfg: None
        self.zt.outbox_flush = lambda cfg: None
        self.zt.busy_fresh = lambda: False
        self.zt.register_bot_menu = lambda cfg: True
        self.zt.load_config = lambda: _cfg(self.zt)
        self.zt.housekeeping = lambda: None
        self.pwlogs = []
        self.zt.pwlog = lambda *a: self.pwlogs.append(" ".join(map(str, a)))
        self.zt.save_offset = lambda o: None

    async def test_mensaje_de_grupo_se_registra_y_ignora(self):
        zt = self.zt
        batch = [{"message_id": 2, "text": "hola desde el grupo",
                  "chat": {"id": -100123}, "from": {"id": 9}}]
        llamadas = {"n": 0}

        def fake_fetch(cfg, offset, timeout):
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                return 50, batch, 1
            raise KeyboardInterrupt
        zt.fetch_updates = fake_fetch
        try:
            zt.cmd_watch_commands()
        except KeyboardInterrupt:
            pass
        self.assertEqual(self.sends, [])  # NO responde al grupo
        self.assertTrue(any("otro chat" in p for p in self.pwlogs))


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
        self.zt.send = lambda cfg, text, loud=None, markup=None: (
            self.enviados.append(text), True)[1]
        self.zt.set_active_bot_session(
            {"sid": support.T1, "title": "testing", "ws": "/w"})
        self.esperas = []
        self.respuesta = None  # lo que "devuelve el canal" (o None: timeout)

        def wait_stub(rid, timeout=None):
            self.esperas.append(rid)
            return self.respuesta
        self.zt.QUESTIONS.wait = wait_stub

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
        zt.QUESTIONS.ask("rid-en-curso", self._preguntas()["questions"],
                         "Alcance", support.T1)
        self.respuesta = "Agresivo (todo)"
        r2 = h(self._preguntas())
        self.assertEqual(self.esperas, ["rid-en-curso"])  # espera la EXISTENTE
        self.assertEqual(self.enviados, [])   # cero preguntas nuevas al chat
        self.assertEqual(r2["content"]["answer"], "Agresivo (todo)")
        self.assertEqual(len(zt.QUESTIONS.pendings()), 1)  # no duplicó canal

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
