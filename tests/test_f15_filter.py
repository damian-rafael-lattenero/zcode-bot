"""F15 — solo la task current habla al chat.

Pedido de Damian 2026-10-13: "quiero que lleguen novedades, mensajes y
avisos SOLAMENTE de la TASK CURRENT, del resto no". El sistema no tenía NI
UN filtro de sesión: los hooks disparaban para cualquier runtime (incluida
la app corriendo otras tasks), cada turno del celular producía doble-stream
(hooks ▶️/✅ ENCIMA de las cajas del worker — verificado en payloads con
HEADLESS_GUARD), un /task switch a mitad de turno no cortaba el stream, y
la cola relanzaba turnos de sesiones que dejaron de ser la activa.

Reglas cubiertas:
- push no solicitado de otra task → silencio (marker igual se limpia).
- permiso/pregunta de otra task → ni mensaje NI espera (gana el diálogo de
  la Mac; antes retenía el tool call hasta 240 s).
- sesión con worker propio → hooks silenciados (el worker ya reporta).
- respuesta final del turno → SIEMPRE llega (no se testea acá: es la regla
  de prompt_worker, el gate vive solo en _live_view).
- cola de task inactiva duerme; /task vuelve → auto-resume.
- preguntas ❓ enrutadas por sid (la task current primero).
"""
import json
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

from zcode_bot import concurrency  # noqa: E402


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # F4j: los on_* viven en hooks — TODOS los stubs van al módulo
        # dueño (el transporte al seam compartido zc_http)
        from zcode_bot import hooks as zc_hooks
        from zcode_bot.utils import http as zc_http
        self.zt = support.fresh(self)
        self.T1 = support.T1  # activa por defecto (fixture bot-session.json)
        self.T2 = support.T2  # CV — otra task
        self.sends = []
        self.emitted = []
        # on_user_prompt no debe spawnear procesos reales de stuck-watch
        self.popen = []
        self.wait_calls = []

        def fake_wait(cfg, hint):
            self.wait_calls.append(hint)
            return ("yes", "si")
        patches = [
            mock.patch.object(
                zc_http, "send",
                lambda cfg, text, loud=None, markup=None:
                    (self.sends.append(text), True)[1]),
            mock.patch.object(zc_http, "_live_send", lambda cfg, text: 555),
            mock.patch.object(
                zc_http, "_live_edit",
                lambda cfg, mid, text:
                    (self.sends.append(("edit", text)), True)[1]),
            mock.patch.object(zc_hooks, "emit",
                              lambda obj: self.emitted.append(obj)),
            mock.patch.object(
                zc_hooks, "subprocess", types.SimpleNamespace(
                    Popen=lambda *a, **k: self.popen.append(a),
                    DEVNULL=subprocess.DEVNULL)),
            mock.patch.object(zc_hooks, "wait_reply", fake_wait),
        ]
        for pp in patches:
            pp.start()
            self.addCleanup(pp.stop)

    def _p(self, sid, **kw):
        p = {"sessionId": sid, "cwd": "/fake/proj"}
        p.update(kw)
        return p


class TestHooksOtraTask(_Base):
    """Push de una task que NO es la current: silencio total."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)

    async def test_prompt_ajeno_no_notifica_pero_marcador_si(self):
        zt = self.zt
        zt.on_user_prompt(self.cfg, self._p(self.T2, prompt="hola CV"))
        self.assertEqual(self.sends, [])
        # el stuck-watch SÍ arranca (si la task vuelve a ser current y está
        # trabada, avisa): lo gateado es el send, no el ciclo del marker
        self.assertEqual(len(self.popen), 1)
        self.assertTrue(os.path.exists(zt.marker_path(self.T2)))
        zt.on_stop(self.cfg, self._p(self.T2, responsePreview="listo"))
        self.assertEqual(self.sends, [])
        self.assertFalse(os.path.exists(zt.marker_path(self.T2)))

    async def test_fallo_y_sesion_ajenos_silenciados(self):
        zt = self.zt
        zt.on_tool_failure(self.cfg, self._p(self.T2, tool_name="Bash",
                                             error="segfault raro"))
        zt.on_session_start(self.cfg, self._p(self.T2, source="resume"))
        self.assertEqual(self.sends, [])

    async def test_permiso_ajeno_no_manda_ni_espera(self):
        # antes: 🔐 + wait_reply reteniendo el tool call hasta 240 s de una
        # task que ni se estaba mirando (y compitiendo por getUpdates)
        self.zt.on_permission_request(self.cfg, self._p(self.T2))
        self.assertEqual(self.sends, [])
        self.assertEqual(self.wait_calls, [])

    async def test_pregunta_ajena_no_avisa(self):
        self.zt.on_ask_user_question(self.cfg, self._p(
            self.T2, tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "¿Seguro?"}]}))
        self.assertEqual(self.sends, [])

    async def test_sin_sessionid_silencia(self):
        self.zt.on_session_start(self.cfg, {"source": "startup"})
        self.assertEqual(self.sends, [])

    async def test_sin_task_activa_silencia(self):
        self.zt.set_active_bot_session({})
        self.zt.on_session_start(self.cfg, self._p(self.T2, source="startup"))
        self.assertEqual(self.sends, [])

    async def test_flag_apaga_el_filtro(self):
        cfg = _cfg(self.zt, only_active_task=False)
        self.zt.on_session_start(cfg, self._p(self.T2, source="startup"))
        self.assertEqual(len(self.sends), 1)  # comportamiento previo


class TestHooksTaskCurrent(_Base):
    """La task current (corriendo en la app, sin worker del bot) sí notifica."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)

    async def test_prompt_notifica_y_spawnea_stuck_watch(self):
        zt = self.zt
        zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="arreglá X"))
        self.assertEqual(len(self.sends), 1)
        self.assertIn("Turno iniciado", self.sends[0])
        # stuck-watch + espectador (F33: el turno de la Mac streamea al celu)
        self.assertEqual(len(self.popen), 2)
        zt.on_stop(self.cfg, self._p(self.T1, responsePreview="hecho"))
        self.assertEqual(len(self.sends), 2)
        self.assertIn("Turno terminado", self.sends[1])


class TestTurnoCompleto(_Base):
    """F32 — pedido de Damian: el turno de la Mac llega COMPLETO al celu
    (prompt y respuesta íntegros, troceados al límite de Telegram), con
    nombre de task, pasos, duración y workspace en la cabecera."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)

    def test_prompt_completo_en_partes(self):
        prompt = "x" * 5000
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt=prompt))
        self.assertEqual(len(self.sends), 2)  # 5000 chars → 2 partes
        self.assertIn("Turno iniciado", self.sends[0])
        self.assertTrue(self.sends[0].endswith("(1/2)"))
        juntado = (self.sends[0].split("\n", 2)[2].rsplit(" (", 1)[0]
                   + self.sends[1].split("\n", 1)[1].rsplit(" (", 1)[0])
        self.assertEqual(juntado, prompt)

    def test_respuesta_completa_con_datos(self):
        resp = "y" * 9000
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="arrancá"))
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText=resp,
                                          toolCallCount=3))
        # 1 del ▶️ + 3 de la respuesta (9000/3800)
        self.assertEqual(len(self.sends), 4)
        fin = self.sends[1]
        self.assertIn("Turno terminado", fin)
        self.assertIn("🧰 3 pasos", fin)
        self.assertIn("⏱ 0 min", fin)  # marker sin edad → 0
        self.assertIn("📁proj", fin)
        cuerpo = "".join(s.split("\n", 2)[-1].rsplit(" (", 1)[0]
                         for s in self.sends[1:])
        self.assertEqual(cuerpo, resp)

    def test_duracion_desde_marker(self):
        import time as _t
        marker = self.zt.marker_path(self.T1)
        with open(marker, "w") as f:
            f.write("{}")
        viejo = _t.time() - 130
        os.utime(marker, (viejo, viejo))
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText="listo"))
        self.assertIn("⏱ 2 min", self.sends[0])

    def test_recorte_tras_partes_max(self):
        resp = "z" * (3800 * 8)  # 8 partes > 6 permitidas
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText=resp))
        self.assertEqual(len(self.sends), 6)
        self.assertIn("(+", self.sends[-1])
        self.assertIn("/cot", self.sends[-1])

    def test_sin_respuesta_avisa(self):
        self.zt.on_stop(self.cfg, self._p(self.T1))
        self.assertEqual(len(self.sends), 1)
        self.assertIn("(sin respuesta)", self.sends[0])

    def test_espectador_spawnea_para_current(self):
        # F33: el turno de la Mac streamea al celu (cajas de tools + narración)
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        self.assertTrue(any("--espectador" in a[0] for a in self.popen))

    def test_espectador_sin_knob_no_spawnea(self):
        self.cfg["mac_espectador"] = False
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        self.assertFalse(any("--espectador" in a[0] for a in self.popen))
        self.assertEqual(len(self.popen), 1)  # stuck-watch sigue igual

    def test_origen_mac_explcito(self):
        # pedido de Damian: diferenciar turnos de la Mac de los del bot
        # (el workspace puede llamarse "bot" y confundir)
        self.zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        self.assertIn("🖥️ Mac", self.sends[0])
        self.zt.on_stop(self.cfg, self._p(self.T1, responseText="listo"))
        self.assertIn("🖥️ Mac", self.sends[-1])

    async def test_fallo_y_sesion_notifican(self):
        zt = self.zt
        zt.on_tool_failure(self.cfg, self._p(self.T1, tool_name="Bash",
                                             error="segfault raro"))
        zt.on_session_start(self.cfg, self._p(self.T1, source="resume"))
        self.assertEqual(len(self.sends), 2)

    async def test_permiso_actual_espera_y_aprueba(self):
        zt = self.zt
        zt.on_permission_request(self.cfg, self._p(
            self.T1, tool_name="Bash", tool_input={"command": "ls"}))
        self.assertTrue(any("Permiso requerido" in s for s in self.sends))
        self.assertEqual(self.wait_calls, ["permiso pendiente"])
        dec = self.emitted[0]["hookSpecificOutput"]["decision"]["behavior"]
        self.assertEqual(dec, "allow")

    async def test_pregunta_actual_avisa_mac_mode(self):
        self.zt.on_ask_user_question(self.cfg, self._p(
            self.T1, tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "¿Front o back?"}]}))
        self.assertEqual(len(self.sends), 1)
        self.assertIn("El agente pregunta", self.sends[0])
        self.assertEqual(self.wait_calls, [])  # mac mode: avisa y retorna


class TestWorkerSinDobleStream(_Base):
    """Sesión con worker del bot: los hooks NO duplican su reporting."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)
        self.zt._workers().register(self.T1, {"t0": time.time(), "titulo": "testing"})

    async def test_todos_los_hooks_silenciados(self):
        zt = self.zt
        zt.on_user_prompt(self.cfg, self._p(self.T1, prompt="x"))
        zt.on_stop(self.cfg, self._p(self.T1, responsePreview="y"))
        zt.on_session_start(self.cfg, self._p(self.T1, source="resume"))
        zt.on_permission_request(self.cfg, self._p(self.T1, tool_name="Bash"))
        zt.on_ask_user_question(self.cfg, self._p(
            self.T1, tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "¿?"}]}))
        self.assertEqual(self.sends, [])
        self.assertEqual(self.popen, [])
        self.assertEqual(self.wait_calls, [])
        # el marker igual vive su ciclo (side-effect ≠ notificación)
        self.assertFalse(os.path.exists(zt.marker_path(self.T1)))


class TestTaskCurrentHelper(_Base):
    """_task_current_ok: la vigencia se lee EN VIVO (un /task switch corta)."""

    async def test_reglas_basicas(self):
        zt = self.zt
        cfg = _cfg(zt)
        self.assertTrue(zt._task_current_ok(cfg, self.T1))
        self.assertFalse(zt._task_current_ok(cfg, self.T2))
        self.assertFalse(zt._task_current_ok(cfg, None))
        self.assertTrue(zt._task_current_ok(_cfg(zt, only_active_task=False),
                                            self.T2))

    async def test_el_switch_corta_en_vivo(self):
        zt = self.zt
        cfg = _cfg(zt)
        self.assertTrue(zt._task_current_ok(cfg, self.T1))
        zt.set_active_bot_session({"sid": self.T2, "title": "CV",
                                   "ws": support.PROJ_CV})
        self.assertFalse(zt._task_current_ok(cfg, self.T1))
        self.assertTrue(zt._task_current_ok(cfg, self.T2))


class _FakeClient:
    def __init__(self):
        self.max_frame = 0

    def request(self, method, params=None, timeout=None):
        return {"result": {"events": []}}


class _Th:
    """Simula el poller de fin de turno: muere tras N chequeos."""
    
    def __init__(self, vueltas=3):
        self.n = vueltas
    
    def is_alive(self):
        self.n -= 1
        return self.n > 0


class _ThFast:
    """Versión rápida para tests: 1 vuelta, sin sleep."""
    
    def __init__(self):
        self.done = False
    
    def is_alive(self):
        if self.done:
            return False
        self.done = True
        return True


class TestLiveViewPausa(_Base):
    """Al dejar de ser la task current a mitad de turno: UN edit ⏸️ y cero
    stream (la respuesta final la manda prompt_worker igual).

    F4d: contra LiveTurnView con deps inyectadas — sin parchear time.sleep
    global (el seam ahora es el constructor).
    """

    def _run(self):
        from zcode_bot.live_view import LiveTurnView
        sends = []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (sends.append(text), True)[1],
            "live_send": lambda cfg, text: 555,
            "live_edit": lambda cfg, mid, text:
                (sends.append(("edit", text)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c, sid, after_id=None, limit=40: [],
            "poll_cadence": lambda mf: 0.0,
            "rss": lambda: -1,
            "rss_now": lambda: -1,
        }
        import time as _t
        return sends, LiveTurnView(
            _FakeClient(), _cfg(self.zt), self.T1, _t.time(), "testing",
            777, _ThFast(), last_id=None, deps=deps).run()

    async def test_pausa_con_un_solo_edit(self):
        self.zt.set_active_bot_session({"sid": self.T2, "title": "CV",
                                        "ws": support.PROJ_CV})
        sends, _lr = self._run()
        edits = [s for s in sends if isinstance(s, tuple)]
        normales = [s for s in sends if not isinstance(s, tuple)]
        self.assertEqual(normales, [])          # sin CoT ni avisos
        self.assertEqual(len(edits), 1)         # UNA despedida, no lluvia
        self.assertIn("segundo plano", edits[0][1])

    async def test_sin_pausa_sigue_streameando(self):
        sends, _lr = self._run()
        edits = [s for s in sends if isinstance(s, tuple)]
        self.assertGreaterEqual(len(edits), 1)
        self.assertNotIn("segundo plano", edits[0][1])


class TestColaDormidaYResume(_Base):
    """La cola de una task inactiva duerme; /task la retoma solo."""

    def setUp(self):
        super().setUp()
        self.cfg = _cfg(self.zt)
        self.act1 = {"sid": self.T1, "title": "testing", "ws": support.PROJ_BOT}
        self.act2 = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.fired = threading.Event()
        self.lanzados = []

        def spy_spawn(cfg, act, text):
            self.lanzados.append((act.get("sid"), text))
            self.fired.set()
            return object()  # spawn "ok"
        # F4g: _queue_launch_next/_queue_resume viven en workers — el spy
        # cubre ambos módulos (gateway aún consume en handle_free_text)
        support.parchea(self, self.zt, "_spawn_worker", spy_spawn)
        from zcode_bot import workers as zc_workers
        pw = mock.patch.object(zc_workers, "_spawn_worker", spy_spawn)
        pw.start()
        self.addCleanup(pw.stop)

    async def test_drain_no_relanza_sesion_inactiva(self):
        zt = self.zt
        from zcode_bot import workers as zc_workers
        lanzadas = []
        pp = mock.patch.object(zc_workers, "_queue_launch_next",
                               lambda cfg, sid: lanzadas.append(sid))
        pp.start()
        self.addCleanup(pp.stop)
        zt._queue_put(self.T2, self.act2, "seguir con el CV")
        zt._queue_drain(self.cfg, self.T2)
        self.assertEqual(lanzadas, [])
        self.assertEqual(len(zt._queue_peek(self.T2)), 1)  # intacta

    async def test_drain_relanza_la_activa(self):
        zt = self.zt
        # T1 está CORRIENDO en el fixture (busy) → usar T2 (completada):
        # el drain solo relanza sesiones libres y current (F17)
        zt.set_active_bot_session(self.act2)
        zt._queue_put(self.T2, self.act2, "seguir")
        zt._queue_drain(self.cfg, self.T2)
        self.assertTrue(self.fired.wait(3))
        self.assertEqual(self.lanzados, [(self.T2, "seguir")])

    async def test_resume_lanza_y_avisa(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)
        zt._queue_put(self.T2, self.act2, "uno")
        zt._queue_put(self.T2, self.act2, "dos")
        resp = zt._queue_resume(self.cfg, self.T2, "CV")
        self.assertTrue(self.fired.wait(3))
        self.assertEqual(self.lanzados, [(self.T2, "uno")])
        self.assertEqual(len(zt._queue_peek(self.T2)), 1)
        self.assertIn("Retomé", resp)
        self.assertIn("1 más", resp)

    async def test_resume_con_worker_vivo_no_duplica(self):
        zt = self.zt
        zt.set_active_bot_session(self.act2)
        zt._workers().register(self.T2, {"t0": time.time(), "titulo": "CV"})
        zt._queue_put(self.T2, self.act2, "uno")
        zt._queue_put(self.T2, self.act2, "dos")
        resp = zt._queue_resume(self.cfg, self.T2, "CV")
        self.assertFalse(self.fired.is_set())
        self.assertIn("turno corriendo", resp)
        self.assertEqual(len(zt._queue_peek(self.T2)), 2)

    async def test_task_select_integra_resume(self):
        zt = self.zt
        zt._queue_put(self.T2, self.act2, "uno del CV")
        reply = zt._task_select_sid(self.T2)
        self.assertIn("🎯", reply)
        self.assertTrue(self.fired.wait(3))
        self.assertIn("Retomé", reply)


class TestPreguntasPorSid(unittest.IsolatedAsyncioTestCase):
    """Concorrencia: la respuesta va a la sesión que preguntó (F16: por
    archivos — QuestionFiles, el canal worker↔poller)."""

    def setUp(self):
        import tempfile
        self.broker = concurrency.QuestionFiles(
            tempfile.mkdtemp(prefix="zcq15-"))

    async def test_prioridad_por_sid(self):
        b = self.broker
        b.ask("r1", [{"question": "A?"}], "T1", sid=support.T1)
        b.ask("r2", [{"question": "B?"}], "T2", sid=support.T2)
        self.assertEqual(b.first_pending(support.T2)["request_id"], "r2")
        self.assertEqual(b.first_pending(support.T1)["request_id"], "r1")
        # sid sin pregunta propia → fallback a la primera (comportamiento previo)
        self.assertEqual(b.first_pending("sess_OTRA")["request_id"], "r1")
        self.assertEqual(b.first_pending()["request_id"], "r1")
        self.assertTrue(b.answer("r2", "opción 2"))
        restantes = b.pendings()
        self.assertEqual([q["request_id"] for q in restantes], ["r1"])

    async def test_sid_default_none_retrocompatible(self):
        b = self.broker.ask("r1", [{"question": "A?"}], "T1")
        self.assertIsNone(b["sid"])
        self.assertEqual(
            self.broker.first_pending(support.T1)["request_id"], "r1")


if __name__ == "__main__":
    unittest.main()
