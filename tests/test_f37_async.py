"""F37 — el bot nunca espera: drain autónomo de colas + comandos async.

Dos mitades del mismo principio (directiva de Damian: "todo lo que
entra del celular es async, nunca esperar a lo que está en curso"):

- F37a cola_check: la cola dormida F17 esperaba el on_stop de un turno
  de la Mac que puede NUNCA existir (la app tragó el prompt con la task
  ocupada — incidente 19/10 15:47, 88 minutos de sueño). Ahora el
  poller despierta las colas por sí mismo cuando la sesión está libre.
- F37b handle_command async: los comandos lentos (compact/ctx/usage/
  quota/stop) corren en thread con acuse instantáneo — el poller nunca
  bloquea y los demás mensajes siguen entrando.

Lección de la casa (aplicada): los guards de cola_check viven en el
módulo workers COMPARTIDO (el zt de support.fresh es una copia de
gateway) — se parchea zc_workers, no zt. Parchear el lado equivocado
ejecutó el _spawn_worker REAL (el Popen falló contenido en el sandbox,
sin efecto, pero fue suerte).
"""
import threading
import unittest
from unittest import mock

import support
from zcode_bot.commands import dispatch as zc_dispatch
from zcode_bot.utils import http as zc_http
from zcode_bot import workers as zc_workers


class TestColaCheck(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        # throttle propio: fresh por test (es estado de módulo compartido)
        support.parchea(self, zc_workers, "_drain_ts", [0.0])
        self.resumes = []
        self.enviados = []
        support.parchea(self, zc_workers, "_queue_resume",
                        lambda cfg, sid, titulo=None:
                        (self.resumes.append(sid) or "⏳ Retomé"))
        support.parchea(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.enviados.append((text, loud)), True)[1])
        support.parchea(self, zc_workers, "_nuclear_active", lambda: False)
        support.parchea(self, zc_workers, "_task_current_ok",
                        lambda cfg, sid: True)
        support.parchea(self, zc_workers, "running_turns",
                        lambda max_age=7200: {})
        self.act = {"sid": support.T1, "title": "t", "ws": "/w"}

    def test_despierta_cola_dormida_de_sesion_libre(self):
        zc_workers._queue_put(support.T1, self.act, "hola desde la cola")
        zc_workers.cola_check({}, cada=0)
        self.assertEqual(self.resumes, [support.T1])
        self.assertEqual(self.enviados, [("⏳ Retomé", False)])

    def test_throttle_propio_la_segunda_llamada_no_corre(self):
        zc_workers._queue_put(support.T1, self.act, "hola")
        zc_workers.cola_check({}, cada=999)
        zc_workers.cola_check({}, cada=999)  # dentro de la ventana: skip
        self.assertEqual(self.resumes, [support.T1])

    def test_sesion_ocupada_duerme(self):
        """El guard central: si la Mac tiene turno en curso, la cola
        espera (la despertará su on_stop o el próximo chequeo)."""
        zc_workers._queue_put(support.T1, self.act, "hola")
        support.parchea(self, zc_workers, "running_turns",
                        lambda max_age=7200: {support.T1: 123.0})
        zc_workers.cola_check({}, cada=0)
        self.assertEqual(self.resumes, [])

    def test_worker_vivo_duerme(self):
        zc_workers._queue_put(support.T1, self.act, "hola")
        support.parchea(self, zc_workers, "_workers",
                        lambda: type("R", (), {"get": lambda s, k: {"pid": 1}})())
        zc_workers.cola_check({}, cada=0)
        self.assertEqual(self.resumes, [])

    def test_nuclear_frena_todo(self):
        zc_workers._queue_put(support.T1, self.act, "hola")
        support.parchea(self, zc_workers, "_nuclear_active", lambda: True)
        zc_workers.cola_check({}, cada=0)
        self.assertEqual(self.resumes, [])

    def test_no_current_task_duerme(self):
        """F15: la cola de una sesión que no es la task current espera su
        /task — el chequeo NO la despierta por su cuenta."""
        zc_workers._queue_put(support.T1, self.act, "hola")
        support.parchea(self, zc_workers, "_task_current_ok",
                        lambda cfg, sid: False)
        zc_workers.cola_check({}, cada=0)
        self.assertEqual(self.resumes, [])


class TestComandosAsync(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.enviados = []
        support.parchea(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.enviados.append((text, loud)), True)[1])

    def test_comando_lento_corre_en_thread_con_acuse(self):
        """El poller recibe el ACUSE como valor de retorno (sigue el loop
        al toque); el informe viaja después por el thread."""
        enviado = threading.Event()

        def send_fake(cfg, text, loud=None, markup=None):
            self.enviados.append((text, loud))
            enviado.set()
            return True
        support.parchea(self, zc_http, "send", send_fake)
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch",
                                  lambda cfg, text: "📋 el informe de /ctx"):
            r = zc_dispatch.handle_command({}, "/ctx")
        self.assertEqual(r, zc_dispatch.ACK_SLOW)
        self.assertTrue(enviado.wait(timeout=5))
        self.assertEqual(self.enviados, [("📋 el informe de /ctx", False)])

    def test_error_del_thread_reporta_no_muere(self):
        visto = threading.Event()

        def explota(cfg, text):
            visto.set()
            raise ValueError("runtime colgado")
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch", explota):
            zc_dispatch.handle_command({}, "/ctx")
        sonó = threading.Event()

        def send_fake(cfg, text, loud=None, markup=None):
            self.enviados.append((text, loud))
            sonó.set()
            return True
        support.parchea(self, zc_http, "send", send_fake)
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch", explota):
            zc_dispatch.handle_command({}, "/ctx")
        self.assertTrue(sonó.wait(timeout=5))
        self.assertTrue(any("Error en /ctx" in t and "runtime colgado" in t
                            for t, _l in self.enviados))
        self.assertTrue(any(l for _t, l in self.enviados))  # el error SUENA

    def test_comando_rapido_sigue_sincronico(self):
        """Los instantáneos (task/mode/help/...) NO pasan por thread: la
        respuesta sigue siendo el valor de retorno, como siempre."""
        with mock.patch.object(zc_dispatch, "SLOW_ASYNC", True), \
                mock.patch.object(zc_dispatch.REG, "dispatch",
                                  mock.MagicMock(return_value="🏓 pong")) as d:
            r = zc_dispatch.handle_command({}, "/ping")
        self.assertEqual(r, "🏓 pong")
        self.assertEqual(d.call_count, 1)

    def test_modo_sync_de_la_suite(self):
        """support.fresh apaga SLOW_ASYNC: los goldens siguen leyendo el
        valor de retorno del handler, sin threads."""
        self.assertFalse(zc_dispatch.SLOW_ASYNC)
        with mock.patch.object(zc_dispatch.REG, "dispatch",
                               lambda cfg, text: "directo"):
            self.assertEqual(zc_dispatch.handle_command({}, "/ctx"), "directo")


if __name__ == "__main__":
    unittest.main()
