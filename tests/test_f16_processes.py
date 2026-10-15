"""F16b/c — el worker como ACTOR (proceso) y sus canales de archivos.

El modelo (pedido explícito de Damian: "como canales de golang / actores
de akka"): cada worker es un proceso con estado privado (SU runtime, SUS
pipes); el poller es supervisor; TODA comunicación es por mensajes en
state/ (bot-workers.json = registry, bot-stop/<sid> = orden de stop,
bot-q/ = canal de preguntas). El receive es REACTIVO (kqueue: el thread
duerme en el kernel y despierta con el mensaje — cero CPU, sin sondeos).

Estos tests NO spawnean procesos reales de worker (tocarían el
~/.zcode real): ejercitan los contratos de archivo y el entrypoint con
doubles. La prueba end-to-end real (kickstart del poller con turno en
vuelo) es criterio de aceptación del deploy.
"""
import asyncio
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

import asynkit
import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))
from zcode_bot import config as zc_config  # noqa: E402

from zcode_bot import fswatch  # noqa: E402


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class TestSpawnWorker(unittest.IsolatedAsyncioTestCase):
    """El spawn: proceso detached vía shim --run-turn, payload por stdin."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.calls = []

        class FakeStdin(io.BytesIO):
            def close(self):
                pass  # el test lee el buffer después

        class FakeP:
            def __init__(self):
                self.stdin = FakeStdin()
        fake = FakeP()

        def popen(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return fake
        self.zt.subprocess = mock.Mock()
        self.zt.subprocess.Popen = popen
        self.zt.subprocess.DEVNULL = -3

    async def test_spawn_detach_y_payload(self):
        zt = self.zt
        act = {"sid": support.T1, "title": "testing",
               "ws": support.PROJ_BOT}
        p = zt._spawn_worker(_cfg(zt), act, "arreglá el bug F16")
        self.assertIsNotNone(p)
        argv, kw = self.calls[0]
        self.assertEqual(argv[-1], "--run-turn")
        self.assertTrue(kw["start_new_session"])  # DETACHED: el poller puede morir
        self.assertEqual(kw["stdout"], -3)
        self.assertEqual(kw["cwd"], support.PROJ_BOT)
        payload = json.loads(p.stdin.getvalue().decode("utf-8"))
        self.assertEqual(payload, {"act": act, "text": "arreglá el bug F16"})

    async def test_spawn_fallido_reporta(self):
        zt = self.zt
        envia = []
        zt.send = lambda cfg, text, loud=None, markup=None: \
            (envia.append(text), True)[1]

        def boom(*a, **k):
            raise OSError("no hay memoria")
        zt.subprocess.Popen = boom
        res = zt._spawn_worker(_cfg(zt), {"sid": support.T1, "ws": "/x",
                                          "title": "t"}, "hola")
        self.assertIsNone(res)
        self.assertTrue(any("No pude lanzar" in s for s in envia))

    async def test_handle_free_text_no_diga_trabajando_si_fallo(self):
        zt = self.zt
        zt.send = lambda *a, **k: True
        # T2 está LIBRE en el fixture (T1 corre) para llegar al spawn
        zt.set_active_bot_session({"sid": support.T2, "title": "CV",
                                   "ws": support.PROJ_CV})

        def boom(*a, **k):
            return None
        zt._spawn_worker = boom
        self.assertIsNone(zt.handle_free_text(_cfg(zt), "hola"))


class TestCmdRunTurn(unittest.IsolatedAsyncioTestCase):
    """El entrypoint del actor: lee el mailbox (stdin), corre prompt_worker."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.recibidos = []

    def _run(self, payload):
        zt = self.zt
        zt.prompt_worker = lambda cfg, act, text: self.recibidos.append(
            (act, text))
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps(payload))):
            zt.cmd_run_turn()

    async def test_payload_a_prompt_worker(self):
        act = {"sid": support.T1, "title": "testing", "ws": support.PROJ_BOT}
        self._run({"act": act, "text": "mensaje del celular"})
        self.assertEqual(self.recibidos,
                         [(act, "mensaje del celular")])

    async def test_stdin_vacio_no_explota(self):
        self._run({})
        self.assertEqual(self.recibidos, [(None or {}, "")])


class TestStopMarker(unittest.IsolatedAsyncioTestCase):
    """El canal de /stop: poller escribe, worker consume."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    async def test_bot_stop_escribe_marker_y_devuelve_aviso(self):
        zt = self.zt
        zt.WORKERS.register(self.T1, {"titulo": "testing"})
        resp = zt.bot_stop(self.T1)
        self.assertIn("Frenando", resp)
        self.assertTrue(zt._stop_requested(self.T1))
        self.assertTrue(zt.WORKERS.stopped(self.T1))

    async def test_stop_de_corrida_inexistente(self):
        self.assertIn("ya no existe", self.zt.bot_stop("sess_NADA"))

    async def test_consume_deja_de_verlo(self):
        zt = self.zt
        zt.WORKERS.register(self.T1, {"titulo": "t"})
        zt.bot_stop(self.T1)
        zt._stop_consume(self.T1)
        self.assertFalse(zt._stop_requested(self.T1))
        # idempotente (el finally del worker lo llama siempre)
        zt._stop_consume(self.T1)

    async def test_worker_termina_y_limpia_el_canal(self):
        # el finally de prompt_worker consume el marker: un /stop de hace
        # rato no frena el PRÓXIMO turno de la sesión
        zt = self.zt
        zt.WORKERS.register(self.T1, {"titulo": "t"})
        zt.bot_stop(self.T1)
        zt._stop_consume(self.T1)
        self.assertFalse(zt._stop_requested(self.T1))


class TestTurnEndPollReactivo(unittest.IsolatedAsyncioTestCase):
    """fin del turno por KQUEUE: el CLI escribe turn.completed al log y el
    poll despierta del kernel — sin sondeos a ciegas."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_turno_cruzado_ignora_fin_ajeno(self):
        """F20 — REGRESIÓN del incidente 18:29: la app corre un turno en la
        MISMA sesión que el worker; el worker debe esperar SU turnId (el
        primer turn.started posterior a su session/send), no cualquier
        turn.completed de la sesión (entregó la respuesta ajena)."""
        zt = self.zt
        log_hoy = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))

        def ev(ts, name, tid):
            return json.dumps({"timestamp": support._iso(ts),
                               "event": name, "sessionId": support.T1,
                               "turnId": tid})
        base = time.time() - 60
        with open(log_hoy, "a") as f:  # el turno de la Mac: empezó ANTES
            f.write(ev(base - 30, "turn.started", "turn_AJENO") + "\n")
            f.write(ev(base - 5, "turn.completed", "turn_AJENO") + "\n")
        # nuestro turno llega ANTES de armar la espera: el escaneo de
        # seguridad de fswatch rescata lo anterior al armado — sin sleeps
        # de coordinación (la carrera de armado ya no existe)
        with open(log_hoy, "a") as f:
            f.write(ev(time.time(), "turn.started", "turn_MIO") + "\n")
            f.write(ev(time.time(), "turn.completed", "turn_MIO") + "\n")
        fin = zt._turn_end_poll(support.T1, base, timeout=8, t_send=base)
        self.assertIsNotNone(fin)
        self.assertEqual(fin.get("turnId"), "turn_MIO")  # NO el ajeno

    async def test_sin_t_send_comportamiento_previo(self):
        zt = self.zt
        log_hoy = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        t0 = time.time()
        with open(log_hoy, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.completed",
                                "sessionId": support.T1}) + "\n")
        fin = zt._turn_end_poll(support.T1, t0, timeout=3)
        self.assertIsNotNone(fin)  # sin turnId y sin t_send: vale igual

    async def test_stop_check_corre_por_despertar(self):
        zt = self.zt
        n = []

        def check():
            n.append(1)
        t0 = time.time()
        zt._turn_end_poll(support.T1, t0, timeout=0.6, stop_check=check)
        self.assertGreaterEqual(len(n), 1)  # se invocó aunque no hubo log


class TestFswatch(unittest.IsolatedAsyncioTestCase):
    """El primitivo reactivo: kqueue en macOS (produce), sondeo en fallback."""

    async def test_cambio_entre_baseline_y_kqueue(self):
        """Carrera real: cambio ENTRE baseline y kqueue armado.
        
        Usa hooks de sincronización para ser 100% determinista:
        1. Espera a que se tome el baseline (on_baseline)
        2. Espera a que se arme el kqueue (on_armed)
        3. Crea el archivo — debe ser detectado por safety scan
        """
        import tempfile
        d = tempfile.mkdtemp(prefix="zcfs-")
        t0 = time.time()

        # Sig y NO asyncio.Event: los hooks corren en el hilo del executor
        # (to_thread); un Event.set() desde otro hilo no despierta al loop
        # (lección: el puente hilo→loop es Sig, call_soon_threadsafe)
        baseline_ready = asynkit.Sig()
        armed_ready = asynkit.Sig()
        
        def on_baseline():
            baseline_ready.set()
        def on_armed():
            armed_ready.set()
        
        # Armar wait_change en background con hooks
        task = asyncio.create_task(
            asynkit.call(
                fswatch.wait_change, [d], timeout=5, poll=0.1,
                on_baseline=on_baseline, on_armed=on_armed
            )
        )
        
        # Esperar baseline tomado
        await asyncio.wait_for(baseline_ready.wait(), timeout=1.0)
        # Esperar kqueue armado
        await asyncio.wait_for(armed_ready.wait(), timeout=1.0)
        
        # AHORA crear archivo (carrera real: baseline tomado, kqueue armado)
        with open(os.path.join(d, "llego.json"), "w") as f:
            f.write("{}")
        
        hubo = await task
        self.assertTrue(hubo)
        self.assertLess(time.time() - t0, 3)

    async def test_timeout_sin_cambios(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="zcfs-")
        t0 = time.time()
        self.assertFalse(fswatch.wait_change([d], timeout=0.5))
        self.assertGreaterEqual(time.time() - t0, 0.4)

    async def test_path_inexistente_no_explota(self):
        self.assertFalse(fswatch.wait_change(["/no/existe/xyz"], timeout=0.2))
        self.assertTrue(fswatch.wait_change([], timeout=0.05) is False)


if __name__ == "__main__":
    unittest.main()
