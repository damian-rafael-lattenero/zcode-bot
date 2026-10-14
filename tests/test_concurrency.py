"""Tests de concurrencia: AtomicStore, WorkersRegistry, QuestionFiles
y la REGRESIÓN del deadlock de ProtoClient (2026-10-12) con servidor fake.

Suite async (ADR-009): cero sleeps — la coordinación es reactiva
(asynkit.call/wait_until/Chan); los productores y consumidores escalan
con gather, no con hilos escritos a mano.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import asynkit
import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

from zcode_bot import state as zc_state          # noqa: E402
from zcode_bot import concurrency as zc_conc     # noqa: E402
import zcode_proto as zp                          # noqa: E402


class TestAtomicStore(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zcstate-")
        self.path = os.path.join(self.dir, "estado.json")

    async def test_write_read_roundtrip(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"a": 1, "b": ["x", "y"]})
        self.assertEqual(st.read(), {"a": 1, "b": ["x", "y"]})

    async def test_no_queda_tmp_ni_estado_cortado(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"n": 1})
        # crash simulado: excepción a mitad del update no corrompe
        def boom(cur):
            raise RuntimeError("crash a mitad de escritura")
        with self.assertRaises(RuntimeError):
            st.update(boom, {})
        self.assertEqual(st.read(), {"n": 1})  # intacto
        self.assertEqual([f for f in os.listdir(self.dir) if ".tmp" in f], [])

    async def test_update_rmw(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"count": 0})
        for _ in range(3):
            st.update(lambda d: {"count": d["count"] + 1}, {})
        self.assertEqual(st.read()["count"], 3)

    async def test_update_cross_process(self):
        # 4 "procesos" (executor) × 10 puts concurrentes: no se pierde uno
        st = zc_state.AtomicStore(self.path)
        st.write({"n": 0})

        async def worker():
            def diez():
                for _ in range(10):
                    st.update_cross_process(lambda d: {"n": d["n"] + 1},
                                            {"n": 0})
            await asynkit.call(diez)
        await asyncio.gather(*[worker() for _ in range(4)])
        self.assertEqual(st.read()["n"], 40)


class TestWorkersRegistry(unittest.IsolatedAsyncioTestCase):
    """F16: file-backed (bot-workers.json con flock) — misma API que la era
    de threads, pero sobrevive restarts del poller y poda entradas muertas."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="zcworkers-")
        self.path = os.path.join(self.dir, "bot-workers.json")

    def _r(self):
        return zc_conc.WorkersRegistry(self.path)

    async def test_registro_dedupe_y_rekey(self):
        r = self._r()
        self.assertTrue(r.register("nueva", {"t0": 1}))
        self.assertFalse(r.register("nueva", {"t0": 2}))  # dedupe → encolar
        r.rekey("nueva", "sess_real_1")                  # fix orphan bug
        self.assertIsNone(r.get("nueva"))
        # el ARCHIVO tiene la entrada con pid (contrato F16)
        ent = json.load(open(self.path))["sess_real_1"]
        self.assertEqual(ent["pid"], os.getpid())

    async def test_dos_registries_ven_lo_mismo(self):
        # el caso real: poller y worker-proceso, instancias distintas
        a, b = self._r(), self._r()
        self.assertTrue(a.register("s1", {"titulo": "testing"}))
        self.assertIsNotNone(b.get("s1"))  # b ve el alta de a
        self.assertFalse(b.register("s1", {"titulo": "dup"}))  # dedup cruzado

    async def test_stop_flag(self):
        r = self._r()
        r.register("s1", {"titulo": "testing"})
        got = r.flag_stop("s1")
        self.assertEqual(got, "testing")  # F16: devuelve el titulo
        self.assertTrue(r.stopped("s1"))
        self.assertIsNone(r.flag_stop("s2"))  # no existe
        r.unflag_stop("s1")
        self.assertFalse(r.stopped("s1"))
        r.pop("s1")
        self.assertTrue(r.empty())

    async def test_poda_de_worker_muerto(self):
        # crash del worker sin finally: la entrada queda con PID muerto →
        # cualquier lector la poda (si no: dedup roto para siempre)
        r = self._r()
        muerto = subprocess.Popen([sys.executable, "-c", "pass"])
        muerto.wait()
        st = zc_state.AtomicStore(self.path)
        st.write({"s_muerto": {"pid": muerto.pid, "t0": time.time(),
                               "titulo": "x"}})
        self.assertIsNone(r.get("s_muerto"))
        self.assertTrue(r.empty())

    async def test_poda_por_edad_anti_pid_reuse(self):
        # PID VIVO (proceso ajeno) pero entrada de hace >4h: se poda (el
        # pid puede ser otro proceso reciclado). El propio pid del lector
        # está exento — sus workers son siempre de-fiabilidad-conocida.
        r = self._r()
        vivo = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            st = zc_state.AtomicStore(self.path)
            st.write({"s_vieja": {"pid": vivo.pid,
                                  "t0": time.time() - r.MAX_EDAD_S - 60,
                                  "titulo": "x"}})
            self.assertIsNone(r.get("s_vieja"))
        finally:
            vivo.kill()
            vivo.wait()

    async def test_items_es_snapshot_ordenado(self):
        r = self._r()
        r.register("a", {"t0": 5})
        items = r.items()
        r.register("b", {"t0": 1})
        self.assertEqual(len(items), 1)  # copia, no vista viva
        self.assertEqual([k for k, _ in r.items()], ["b", "a"])  # por t0

    async def test_registro_concurrente_sin_perder_dedupe(self):
        import tempfile
        # 2 procesos reales compitiendo por el mismo sid: gana UNO
        d2 = tempfile.mkdtemp(prefix="zcworkers-race-")
        p = os.path.join(d2, "bot-workers.json")
        zc_state.AtomicStore(p).write({})
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from zcode_bot import concurrency, state;"
            "r = concurrency.WorkersRegistry(%r);"
            "print(1 if r.register('sess_X', {'titulo': 'r'}) else 0)"
            % (os.path.join(TESTS_DIR, "..", "src"), p))
        procs = [subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE) for _ in range(4)]
        outs = [int(pr.communicate(timeout=30)[0].strip()) for pr in procs]
        self.assertEqual(sum(outs), 1)  # exactamente UN ganador
        # el archivo quedó con UNA entrada (la del ganador); los pids de
        # los hijos ya murieron, así que un lector nuevo los PODA (crash
        # sin finally no deja el dedup roto)
        raw = json.load(open(p))
        self.assertEqual(list(raw), ["sess_X"])
        self.assertEqual(len(zc_conc.WorkersRegistry(p).items()), 0)


class TestQuestionFiles(unittest.IsolatedAsyncioTestCase):
    """F16: preguntas ❓ por ARCHIVO entre procesos — semántica de canal
    de Go (send escribe, recv bloquea reactivo hasta que llega)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zcq-")
        self.b = zc_conc.QuestionFiles(self.dir)

    async def test_ask_wait_answer_roundtrip(self):
        # la respuesta puede llegar incluso ANTES de que el recv arme su
        # kqueue: el escaneo de seguridad de fswatch la rescata — sin
        # sleeps de coordinación (la espera bloqueante corre en executor)
        self.b.ask("r1", [{"question": "¿Seguro?"}], "T1")
        recv = asyncio.ensure_future(
            asynkit.call(self.b.wait, "r1", timeout=5, poll=0.2))
        t0 = time.time()
        self.assertTrue(self.b.answer("r1", "opción 2"))
        self.assertEqual(await recv, "opción 2")
        self.assertLess(time.time() - t0, 2.0)  # reactivo, no polls largos
        # el recv consumió sus archivos (sin basura)
        self.assertFalse(os.listdir(self.dir))

    async def test_productores_y_consumidores_escalans(self):
        # N productores preguntan, M resolvedores del pool contestan vía
        # canal: cada respuesta llega a SU pregunta (routing por rid).
        # Subir N/M no cambia el test — más corrutinas en gather.
        N, M = 3, 2
        ch = asynkit.Chan()

        async def productor(i):
            rid = "r%d" % i
            self.b.ask(rid, [{"question": rid}], "t")
            await ch.put(rid)
            return await asynkit.call(self.b.wait, rid, timeout=10, poll=0.2)

        async def resolvedor():
            while True:
                rid = await ch.get()
                self.assertTrue(self.b.answer(rid, "ans-" + rid))
                ch.task_done()

        pool = [asyncio.ensure_future(resolvedor()) for _ in range(M)]
        respuestas = await asyncio.gather(*[productor(i) for i in range(N)])
        await ch.join()  # el canal agotó su trabajo: los resolvedores sobran
        for w in pool:
            w.cancel()
        await asyncio.gather(*pool, return_exceptions=True)
        self.assertEqual(respuestas, ["ans-r%d" % i for i in range(N)])
        self.assertFalse(os.listdir(self.dir))  # canal limpio

    async def test_wait_timeout(self):
        t0 = time.time()
        self.assertIsNone(self.b.wait("r2", timeout=0.5, poll=0.1))
        self.assertGreaterEqual(time.time() - t0, 0.4)

    async def test_dos_preguntas_simultaneas_no_se_cruzan(self):
        # REGRESIÓN del slot global único: dos preguntas → cada respuesta
        # llega a SU pregunta
        self.b.ask("r1", [{"options": [{"value": "uno"}]}], "t1")
        self.b.ask("r2", [{"options": [{"value": "dos"}]}], "t2")
        self.assertTrue(self.b.any_pending())
        # responder r1 no toca r2
        self.assertTrue(self.b.answer("r1", "uno"))
        self.assertEqual(len(self.b.pendings()), 1)  # r2 sigue pendiente
        self.assertEqual(self.b.pendings()[0]["request_id"], "r2")
        self.b.pop("r2")
        self.assertFalse(self.b.any_pending())

    async def test_first_pending_por_sid(self):
        self.b.ask("r1", [{"question": "A?"}], "T1", sid=support.T1)
        self.b.ask("r2", [{"question": "B?"}], "T2", sid=support.T2)
        self.assertEqual(self.b.first_pending(support.T2)["request_id"], "r2")
        self.assertEqual(self.b.first_pending(support.T1)["request_id"], "r1")
        # sid sin pregunta propia → la primera por edad
        self.assertEqual(self.b.first_pending("sess_OTRA")["request_id"], "r1")
        self.assertEqual(self.b.first_pending()["request_id"], "r1")

    async def test_answer_inexistente(self):
        self.assertFalse(self.b.answer("nope", "x"))

    async def test_poda_de_pregunta_huerfana(self):
        # worker crasheado: su q vieja no aparece como pendiente para siempre
        self.b.ask("vieja", [{"question": "A?"}], "T1")
        vieja = self.b._p("q", "vieja")
        obj = json.load(open(vieja))
        obj["t0"] = time.time() - 7200  # 2h
        json.dump(obj, open(vieja, "w"))
        self.assertEqual(self.b.pendings(), [])
        self.assertFalse(self.b.any_pending())


class TestProtoClientRegresionDeadlock(unittest.IsolatedAsyncioTestCase):
    """REGRESIÓN del incidente 2026-10-12: request() anidaba wlock dentro de
    wlock → deadlock, bot mudo 10 min. Con el servidor fake verificamos que
    requests CONCURRENTES de varios hilos completan."""

    def _client(self):
        return zp.ProtoClient(
            argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])

    async def test_requests_concurrentes_completan(self):
        c = self._client()
        try:
            errores = []

            async def golpear(n):
                for i in range(6):
                    r = await asynkit.call(
                        c.request, "probe/metodo%d" % ((n * 6 + i) % 18),
                        {"slow_ms": 10}, timeout=15)
                    if r.get("error") or (r.get("result") or {}).get(
                            "echo") != "probe/metodo%d" % ((n * 6 + i) % 18):
                        errores.append(r)
            t0 = time.time()
            await asyncio.wait_for(
                asyncio.gather(*[golpear(n) for n in range(3)]), timeout=30)
            self.assertEqual(errores, [])
            self.assertLess(time.time() - t0, 20)
        finally:
            c.close()

    async def test_un_request_lento_no_bloquea_al_rapido(self):
        # determinismo por SEÑAL, no por tiempo: fake_server crea el
        # marker cuando el request lento ya está en vuelo en el server
        mark = os.path.join(tempfile.mkdtemp(prefix="zcslow-"), "slow-armed")
        self.addCleanup(os.environ.pop, "ZCFAKE_SLOW_MARK", None)
        os.environ["ZCFAKE_SLOW_MARK"] = mark
        c = self._client()
        try:
            lento = asyncio.ensure_future(
                asynkit.call(c.request, "slow/uno", {"slow_ms": 2000},
                             timeout=15))
            await asynkit.wait_until(
                lambda: os.path.exists(mark),
                msg="el request lento debió arrancar en el server")
            t0 = time.time()
            r = await asynkit.call(c.request, "rapido/dos", {}, timeout=15)
            self.assertLess(time.time() - t0, 1.5)  # no esperó al lento
            self.assertEqual((r.get("result") or {}).get("echo"), "rapido/dos")
            lento_r = await lento
            self.assertEqual((lento_r.get("result") or {}).get("echo"),
                             "slow/uno")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
