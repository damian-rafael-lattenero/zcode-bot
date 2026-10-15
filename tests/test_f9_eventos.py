"""F9 — el fin del turno llega por el STREAM de eventos (ADR-013 follow-up).

_turn_end_poll deja de escanear logs JSONL cuando hay conexión + cursor:
poll incremental afterSeq con correlación por inputId (el stream no trae
turnId — spike scripts/spike_f9_turnfin.py). El escaneo de logs queda
como fallback (conn muerto, frame gigante, sin conn). Regresión F20
incluida: un turn.completed AJENO (otro inputId) jamás se acepta.
"""
import json
import os
import time
import unittest

import support
from zcode_bot import config as zc_config
from zcode_bot import live_view as zc_live


class FakeConn:
    """ProtoClient de mentira: scripted de respuestas de session/events."""

    def __init__(self, batches=None, error=None):
        self.batches = list(batches or [])
        self.error = error
        self.params = []

    def request(self, method, params=None, timeout=200):
        assert method == "session/events", method
        self.params.append(dict(params or {}))
        if self.error:
            raise self.error
        if self.batches:
            return {"result": {"events": self.batches.pop(0)}}
        return {"result": {"events": []}}


def ev_turn(tipo, input_id, ts_ms=None, extra=None):
    p = {"inputId": input_id}
    if ts_ms is not None:
        p["executionStartedAt"] = ts_ms
    if extra:
        p.update(extra)
    return {"type": tipo, "payload": p, "seq": int(time.time() * 1000) % 10 ** 6,
            "eventId": "e-%s-%f" % (input_id, time.time())}


class TestFinDeStream(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.t_send = time.time() - 1

    def test_fin_correlacionado_por_input_id(self):
        # nuestro started (inputId A) → ajeno B completo → completed A
        conn = FakeConn(batches=[[
            ev_turn("turn.started", "A", ts_ms=self.t_send * 1000),
            ev_turn("turn.started", "B", ts_ms=self.t_send * 1000),
            ev_turn("turn.completed", "B", extra={"resultType": "success"}),
            ev_turn("turn.completed", "A", extra={"resultType": "success"}),
        ]])
        fin = self.zt._turn_end_poll(support.T1, time.time() - 30, timeout=5,
                                     t_send=self.t_send, conn=conn,
                                     start_seq=10)
        self.assertIsNotNone(fin)
        self.assertEqual(fin["event"], "turn.completed")
        # el primer poll fue incremental (afterSeq del cursor)
        self.assertEqual(conn.params[0].get("afterSeq"), 10)

    def test_sin_t_send_acepta_cualquiera(self):
        conn = FakeConn(batches=[[
            ev_turn("turn.started", "X", ts_ms=1),
            ev_turn("turn.completed", "X", extra={"resultType": "success"}),
        ]])
        fin = self.zt._turn_end_poll(support.T1, time.time() - 30, timeout=5,
                                     conn=conn, start_seq=0)
        self.assertIsNotNone(fin)
        self.assertEqual(fin["event"], "turn.completed")

    def test_result_type_fallido_es_turn_failed(self):
        conn = FakeConn(batches=[[
            ev_turn("turn.started", "A", ts_ms=self.t_send * 1000),
            ev_turn("turn.completed", "A",
                    extra={"resultType": "error",
                           "error": {"message": "boom del modelo"}}),
        ]])
        fin = self.zt._turn_end_poll(support.T1, time.time() - 30, timeout=5,
                                     t_send=self.t_send, conn=conn,
                                     start_seq=0)
        self.assertEqual(fin["event"], "turn.failed")
        self.assertIn("boom del modelo", fin["error"]["message"])

    def test_completado_ajeno_sin_mios_no_se_acepta(self):
        # F20 en el stream: completed de un peer SIN haber visto nuestro
        # started → no vale (con t_send, mios=None no acepta nada)
        conn = FakeConn(batches=[[
            ev_turn("turn.completed", "AJENO",
                    extra={"resultType": "success"})]])
        fin = self.zt._turn_end_poll(support.T1, time.time() - 30,
                                     timeout=0.4, t_send=self.t_send,
                                     conn=conn, start_seq=0)
        self.assertIsNone(fin)

    def test_stream_caido_degrada_a_logs(self):
        # conn explota en el primer poll → fallback: el fin viene del log
        # del CLI (camino F20 intacto, con turnId)
        log_hoy = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        base = time.time() - 30
        with open(log_hoy, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.started",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG"}) + "\n")
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.completed",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG"}) + "\n")
        conn = FakeConn(error=OSError("pipe roto"))
        fin = self.zt._turn_end_poll(support.T1, base, timeout=5,
                                     t_send=base, conn=conn, start_seq=0)
        self.assertIsNotNone(fin)
        self.assertEqual(fin.get("turnId"), "turn_LOG")

    def test_frame_gigante_degrada_a_logs(self):
        # afterSeq roto (replay de 26MB en un poll incremental) → mismo
        # trip anti-OOM del live: degradar a logs, no parsear GBs
        log_hoy = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        base = time.time() - 30
        with open(log_hoy, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.started",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG2"}) + "\n")
            f.write(json.dumps({"timestamp": support._iso(time.time()),
                                "event": "turn.completed",
                                "sessionId": support.T1,
                                "turnId": "turn_LOG2"}) + "\n")
        conn = FakeConn(batches=[{"result": {"events": [], "pad": "x" * 26000000}}])
        fin = self.zt._turn_end_poll(support.T1, base, timeout=8,
                                     t_send=base, conn=conn, start_seq=0)
        self.assertIsNotNone(fin)
        self.assertEqual(fin.get("turnId"), "turn_LOG2")


class TestFinDeStreamBordes(unittest.TestCase):
    """Bordes del correlacionador PURO (sin conn ni fixture): el par
    started/completed puede identificarse por queryId (sin inputId) en
    cualquiera de las dos puntas, y el error del payload puede venir como
    string, no dict."""

    T_SEND = time.time() - 5

    def _started(self, **pid):
        p = {"executionStartedAt": (time.time() - 4) * 1000}
        p.update(pid)
        return {"type": "turn.started", "seq": 11, "payload": p}

    def test_fallback_query_id_en_las_dos_puntas(self):
        # runtime que identifica el turno por queryId (sin inputId):
        # el started fija mios desde queryId y el completed con el MISMO
        # queryId cierra — antes el matcheo asumía inputId y ese fin no
        # se veía jamás
        completed = {"type": "turn.completed", "seq": 12,
                     "payload": {"queryId": "q-77",
                                 "resultType": "success"}}
        fin, mios, last = zc_live._fin_de_stream(
            [self._started(queryId="q-77"), completed], None,
            self.T_SEND, 0)
        self.assertIsNotNone(fin)
        self.assertEqual(fin["event"], "turn.completed")
        self.assertEqual(mios, "q-77")
        self.assertEqual(last, 12)

    def test_error_como_string_es_turn_failed(self):
        # error crudo (string) en vez de {message: ...}: el mensaje del
        # fin es ese string, no un crash ni un dict vacío
        completed = {"type": "turn.completed", "seq": 12,
                     "payload": {"inputId": "A", "resultType": "error",
                                 "error": "quota agotada"}}
        fin, mios, _ = zc_live._fin_de_stream(
            [self._started(inputId="A"), completed], None,
            self.T_SEND, 0)
        self.assertEqual(fin["event"], "turn.failed")
        self.assertEqual(mios, "A")
        self.assertIn("quota agotada", fin["error"]["message"])

    def test_fallido_sin_error_reporta_el_result_type(self):
        completed = {"type": "turn.failed", "seq": 12,
                     "payload": {"inputId": "A", "resultType": "cancelled"}}
        fin, _mios, _ = zc_live._fin_de_stream(
            [self._started(inputId="A"), completed], None,
            self.T_SEND, 0)
        self.assertEqual(fin["event"], "turn.failed")
        self.assertEqual(fin["error"]["message"], "cancelled")


if __name__ == "__main__":
    unittest.main()
