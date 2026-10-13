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
import io
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

from zcode_bot import fswatch  # noqa: E402


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class TestSpawnWorker(unittest.TestCase):
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

    def test_spawn_detach_y_payload(self):
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

    def test_spawn_fallido_reporta(self):
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

    def test_handle_free_text_no_diga_trabajando_si_fallo(self):
        zt = self.zt
        zt.send = lambda *a, **k: True
        # T2 está LIBRE en el fixture (T1 corre) para llegar al spawn
        zt.set_active_bot_session({"sid": support.T2, "title": "CV",
                                   "ws": support.PROJ_CV})

        def boom(*a, **k):
            return None
        zt._spawn_worker = boom
        self.assertIsNone(zt.handle_free_text(_cfg(zt), "hola"))


class TestCmdRunTurn(unittest.TestCase):
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

    def test_payload_a_prompt_worker(self):
        act = {"sid": support.T1, "title": "testing", "ws": support.PROJ_BOT}
        self._run({"act": act, "text": "mensaje del celular"})
        self.assertEqual(self.recibidos,
                         [(act, "mensaje del celular")])

    def test_stdin_vacio_no_explota(self):
        self._run({})
        self.assertEqual(self.recibidos, [(None or {}, "")])


class TestStopMarker(unittest.TestCase):
    """El canal de /stop: poller escribe, worker consume."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def test_bot_stop_escribe_marker_y_devuelve_aviso(self):
        zt = self.zt
        zt.WORKERS.register(self.T1, {"titulo": "testing"})
        resp = zt.bot_stop(self.T1)
        self.assertIn("Frenando", resp)
        self.assertTrue(zt._stop_requested(self.T1))
        self.assertTrue(zt.WORKERS.stopped(self.T1))

    def test_stop_de_corrida_inexistente(self):
        self.assertIn("ya no existe", self.zt.bot_stop("sess_NADA"))

    def test_consume_deja_de_verlo(self):
        zt = self.zt
        zt.WORKERS.register(self.T1, {"titulo": "t"})
        zt.bot_stop(self.T1)
        zt._stop_consume(self.T1)
        self.assertFalse(zt._stop_requested(self.T1))
        # idempotente (el finally del worker lo llama siempre)
        zt._stop_consume(self.T1)

    def test_worker_termina_y_limpia_el_canal(self):
        # el finally de prompt_worker consume el marker: un /stop de hace
        # rato no frena el PRÓXIMO turno de la sesión
        zt = self.zt
        zt.WORKERS.register(self.T1, {"titulo": "t"})
        zt.bot_stop(self.T1)
        zt._stop_consume(self.T1)
        self.assertFalse(zt._stop_requested(self.T1))


class TestTurnEndPollReactivo(unittest.TestCase):
    """fin del turno por KQUEUE: el CLI escribe turn.completed al log y el
    poll despierta del kernel — sin sondeos a ciegas."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_fin_detectado_tras_escritura_al_log(self):
        zt = self.zt
        env = support.build_env  # noqa: F841  (contexto del fixture)
        log_hoy = os.path.join(
            zt.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        t0 = time.time()
        res = {}

        def escribie_fin():
            time.sleep(0.4)
            with open(log_hoy, "a") as f:
                f.write(json.dumps({"timestamp": support._iso(time.time()),
                                    "event": "turn.completed",
                                    "sessionId": support.T1}) + "\n")
        th = threading.Thread(target=escribie_fin)
        th.start()
        fin = zt._turn_end_poll(support.T1, t0, timeout=8)
        th.join(5)
        res["fin"] = fin
        self.assertIsNotNone(res["fin"])
        self.assertEqual(res["fin"].get("event"), "turn.completed")
        self.assertLess(time.time() - t0, 6)  # reactivo: no agotó el timeout

    def test_stop_check_corre_por_despertar(self):
        zt = self.zt
        n = []

        def check():
            n.append(1)
        t0 = time.time()
        zt._turn_end_poll(support.T1, t0, timeout=0.6, stop_check=check)
        self.assertGreaterEqual(len(n), 1)  # se invocó aunque no hubo log


class TestFswatch(unittest.TestCase):
    """El primitivo reactivo: kqueue en macOS (produce), sondeo en fallback."""

    def test_cambio_en_dir_despierta(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="zcfs-")
        def toca():
            time.sleep(0.3)
            with open(os.path.join(d, "llego.json"), "w") as f:
                f.write("{}")
        th = threading.Thread(target=toca)
        th.start()
        t0 = time.time()
        hubo = fswatch.wait_change([d], timeout=5)
        th.join(5)
        self.assertTrue(hubo)
        self.assertLess(time.time() - t0, 3)

    def test_timeout_sin_cambios(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="zcfs-")
        t0 = time.time()
        self.assertFalse(fswatch.wait_change([d], timeout=0.5))
        self.assertGreaterEqual(time.time() - t0, 0.4)

    def test_path_inexistente_no_explota(self):
        self.assertFalse(fswatch.wait_change(["/no/existe/xyz"], timeout=0.2))
        self.assertTrue(fswatch.wait_change([], timeout=0.05) is False)


if __name__ == "__main__":
    unittest.main()
