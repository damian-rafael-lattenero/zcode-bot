"""Tests de concurrencía: AtomicStore, WorkersRegistry, QuestionsBroker
y la REGRESIÓN del deadlock de ProtoClient (2026-10-12) con servidor fake.
"""
import json
import os
import sys
import threading
import time
import unittest

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

from zcode_bot import state as zc_state          # noqa: E402
from zcode_bot import concurrency as zc_conc     # noqa: E402
import zcode_proto as zp                          # noqa: E402


class TestAtomicStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="zcstate-")
        self.path = os.path.join(self.dir, "estado.json")

    def test_write_read_roundtrip(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"a": 1, "b": ["x", "y"]})
        self.assertEqual(st.read(), {"a": 1, "b": ["x", "y"]})

    def test_no_queda_tmp_ni_estado_cortado(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"n": 1})
        # crash simulado: excepción a mitad del update no corrompe
        def boom(cur):
            raise RuntimeError("crash a mitad de escritura")
        with self.assertRaises(RuntimeError):
            st.update(boom, {})
        self.assertEqual(st.read(), {"n": 1})  # intacto
        self.assertEqual([f for f in os.listdir(self.dir) if ".tmp" in f], [])

    def test_update_rmw(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"count": 0})
        for _ in range(3):
            st.update(lambda d: {"count": d["count"] + 1}, {})
        self.assertEqual(st.read()["count"], 3)

    def test_update_cross_process(self):
        # threads simulan procesos: 40 puts concurrentes no se pierden
        st = zc_state.AtomicStore(self.path)
        st.write({"n": 0})
        def worker():
            for _ in range(10):
                st.update_cross_process(lambda d: {"n": d["n"] + 1}, {"n": 0})
        ths = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ths]
        [t.join(30) for t in ths]
        self.assertEqual(st.read()["n"], 40)


class TestWorkersRegistry(unittest.TestCase):
    def test_registro_dedupe_y_rekey(self):
        r = zc_conc.WorkersRegistry()
        self.assertTrue(r.register("nueva", {"t0": 1}))
        self.assertFalse(r.register("nueva", {"t0": 2}))  # dedupe → encolar
        r.set_client("nueva", "CLIENTE")
        r.rekey("nueva", "sess_real_1")                  # fix orphan bug
        self.assertIsNone(r.get("nueva"))
        self.assertEqual(r.get("sess_real_1")["client"], "CLIENTE")

    def test_stop_flag(self):
        r = zc_conc.WorkersRegistry()
        r.register("s1", {"titulo": "testing", "client": "C"})
        got = r.flag_stop("s1")
        self.assertEqual(got, ("C", "testing"))
        self.assertTrue(r.stopped("s1"))
        self.assertIsNone(r.flag_stop("s2"))  # no existe
        r.pop("s1")
        self.assertTrue(r.empty())

    def test_items_es_snapshot(self):
        r = zc_conc.WorkersRegistry()
        r.register("a", {})
        items = r.items()
        r.register("b", {})
        self.assertEqual(len(items), 1)  # copia, no vista viva


class TestQuestionsBroker(unittest.TestCase):
    def test_dos_preguntas_simultaneas_no_se_cruzan(self):
        # REGRESIÓN del slot global único: dos preguntas → cada respuesta
        # llega a SU pregunta
        b = zc_conc.QuestionsBroker()
        q1 = b.ask("r1", [{"options": [{"value": "uno"}]}], "t1")
        q2 = b.ask("r2", [{"options": [{"value": "dos"}]}], "t2")
        self.assertTrue(b.answer("r2", "dos"))
        self.assertTrue(q2.event.is_set())
        self.assertEqual(q2.answer, "dos")
        self.assertFalse(q1.event.is_set())   # la otra sigue pendiente
        self.assertTrue(b.any_pending())
        self.assertEqual(b.first_pending(), q1)
        b.pop("r2")
        self.assertTrue(b.answer("r1", "uno"))
        self.assertFalse(b.any_pending())

    def test_answer_inexistente(self):
        b = zc_conc.QuestionsBroker()
        self.assertFalse(b.answer("nope", "x"))


class TestProtoClientRegresionDeadlock(unittest.TestCase):
    """REGRESIÓN del incidente 2026-10-12: request() anidaba wlock dentro de
    wlock → deadlock, bot mudo 10 min. Con el servidor fake verificamos que
    requests CONCURRENTES de varios hilos completan."""

    def _client(self):
        return zp.ProtoClient(
            argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])

    def test_requests_concurrentes_completan(self):
        c = self._client()
        try:
            errores = []

            def golpear():
                for i in range(6):
                    r = c.request("probe/metodo%d" % i,
                                  {"slow_ms": 10}, timeout=15)
                    if r.get("error") or (r.get("result") or {}).get(
                            "echo") != "probe/metodo%d" % i:
                        errores.append(r)

            ths = [threading.Thread(target=golpear) for _ in range(3)]
            t0 = time.time()
            [t.start() for t in ths]
            for t in ths:
                t.join(30)
                self.assertFalse(t.is_alive(),
                                 "DEADLOCK: un hilo no terminó (regresión)")
            self.assertEqual(errores, [])
            self.assertLess(time.time() - t0, 20)
        finally:
            c.close()

    def test_un_request_lento_no_bloquea_al_rapido(self):
        c = self._client()
        try:
            res = {}

            def lento():
                res["lento"] = c.request("slow/uno", {"slow_ms": 2000},
                                         timeout=15)
            th = threading.Thread(target=lento)
            th.start()
            time.sleep(0.2)
            t0 = time.time()
            r = c.request("rapido/dos", {}, timeout=15)  # no debe esperar al lento
            self.assertLess(time.time() - t0, 1.5)
            self.assertEqual((r.get("result") or {}).get("echo"), "rapido/dos")
            th.join(20)
            self.assertEqual((res["lento"].get("result") or {}).get("echo"),
                             "slow/uno")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
