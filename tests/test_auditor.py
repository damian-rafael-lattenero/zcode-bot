# -*- coding: utf-8 -*-
"""F30 — invariant auditor: the shared log (audit), failure correlation
("sesión enferma" on the 2nd attempt — incident 18:10), orphaned /stop
markers (the nuclear bounce, 13066 18:13), ghost workers (empty
registry = blind /stop, incident 15:40), dormant queue, key-based
anti-spam and the poller's zombie reaping (13066 defunct)."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import support
from test_prompt_worker import _cfg

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))

from zcode_bot import audit as zc_audit
from zcode_bot import auditor as zc_auditor
from zcode_bot import config as zc_config
from zcode_bot import workers as zc_workers
from zcode_bot.utils import http as zc_http


class _Reloj:
    """Fake time for audit.registrar: ts controlled by the test."""

    def __init__(self, ts):
        self.ts = ts

    def time(self):
        return self.ts


class BaseAuditor(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sid = support.T2
        zc_auditor._ULTIMA[0] = 0.0  # throttle: each test starts clean
        self.sends = []
        ps = [
            mock.patch.object(zc_http, "send",
                              lambda cfg, text, loud=None, markup=None:
                              (self.sends.append(text), True)[1]),
            # isolation from the REAL system: _workers_fantasma scans the
            # Mac's ps and a live HOT worker (turn from the phone in use,
            # THIS test's registry empty due to fresh) showed up as a
            # ghost and broke tests that are not about ghosts. Ghost tests
            # inject their pids_run_turn via parameter.
            mock.patch.object(zc_auditor.zc_nuclear, "_nuc_pids",
                              lambda *a, **k: []),
        ]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def cfg(self, **extra):
        # cadence 1 (positive) by default: knob 0 IS the off switch, so
        # invoking auditar by hand requires a value > 0 — the tests
        # control the cadence with spaced timestamps
        extra.setdefault("auditor_cada_s", 1)
        return _cfg(self.zt, **extra)

    def fallo(self, sid, tipo, ts, detalle="x"):
        p = mock.patch.object(zc_audit, "time", _Reloj(ts))
        p.start()
        self.addCleanup(p.stop)
        zc_audit.registrar(sid, tipo, detalle)


class TestRegistro(BaseAuditor):
    def test_registrar_persiste_y_lee(self):
        self.fallo(self.sid, "proto", 100.0, "timeout setModel")
        evs = zc_audit.leer()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["sid"], self.sid)
        self.assertEqual(evs[0]["tipo"], "proto")
        self.assertEqual(evs[0]["detalle"], "timeout setModel")

    def test_cap_300(self):
        for i in range(320):
            self.fallo(None, "envio", 100.0 + i)
        evs = zc_audit.leer()
        self.assertEqual(len(evs), support.AUDIT_CAP)
        self.assertEqual(evs[0]["ts"], 120.0)  # the first 20 dropped


class TestCorrelacion(BaseAuditor):
    def auditar(self, ahora):
        zc_auditor.auditar(self.cfg(), ahora=ahora)

    def test_un_fallo_no_alerta(self):
        self.fallo(self.sid, "proto", 1000.0)
        self.auditar(1005.0)
        self.assertEqual(self.sends, [])

    def test_dos_fallos_iguales_alertan_sesion_enferma(self):
        self.fallo(self.sid, "proto", 1000.0)
        self.fallo(self.sid, "proto", 1060.0)
        self.auditar(1070.0)
        self.assertEqual(len(self.sends), 1)
        self.assertIn("sesión enferma", self.sends[0])
        self.assertIn("sess_%s" % self.sid[-8:], self.sends[0])
        self.assertIn("proto", self.sends[0])

    def test_fallos_distintos_no_correlacionan(self):
        self.fallo(self.sid, "proto", 1000.0)
        self.fallo(self.sid, "send", 1060.0)
        self.auditar(1070.0)
        self.assertEqual(self.sends, [])

    def test_fallos_fuera_de_ventana_no_alertan(self):
        self.fallo(self.sid, "proto", 100.0)
        self.fallo(self.sid, "proto", 500.0)
        self.auditar(5000.0)  # 600s window: both fell outside
        self.assertEqual(self.sends, [])

    def test_fallos_globales_envio_correlacionan(self):
        self.fallo(None, "envio", 1000.0)
        self.fallo(None, "envio", 1030.0)
        self.auditar(1040.0)
        self.assertEqual(len(self.sends), 1)
        self.assertIn("envíos", self.sends[0])

    def test_cooldown_no_realerta(self):
        self.fallo(self.sid, "proto", 1000.0)
        self.fallo(self.sid, "proto", 1010.0)
        self.auditar(1020.0)
        # identical new pair one minute later: inside the 60m cooldown
        self.fallo(self.sid, "proto", 1060.0)
        self.fallo(self.sid, "proto", 1070.0)
        self.auditar(1080.0)
        self.assertEqual(len(self.sends), 1)

    def test_cooldown_vencido_vuelve_a_alertar(self):
        self.fallo(self.sid, "proto", 0.0)
        self.fallo(self.sid, "proto", 5.0)
        self.auditar(10.0)
        self.fallo(self.sid, "proto", 4000.0)
        self.fallo(self.sid, "proto", 4010.0)
        self.auditar(4020.0)  # > 60 min from the first alert (3600s)
        self.assertEqual(len(self.sends), 2)


class TestMarkersHuerfanos(BaseAuditor):
    def marker(self, sid, edad_s):
        d = os.path.join(zc_config.STATE_DIR, "bot-stop")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, sid)
        with open(p, "w") as f:
            f.write("0.0")
        viejo = time.time() - edad_s
        os.utime(p, (viejo, viejo))
        return p

    def test_huerfano_viejo_se_consumen_silencioso(self):
        p = self.marker(self.sid, edad_s=300)
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertFalse(os.path.exists(p))
        tipos = [e["tipo"] for e in zc_audit.leer()]
        self.assertIn("marker-huerfano", tipos)
        self.assertEqual(self.sends, [])  # silent healing: no alert

    def test_marker_fresco_no_se_toca(self):
        p = self.marker(self.sid, edad_s=5)  # spawn possibly in flight
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertTrue(os.path.exists(p))

    def test_marker_con_worker_vivo_no_se_toca(self):
        zc_workers._workers().register(self.sid, {"titulo": "testing"})
        p = self.marker(self.sid, edad_s=300)
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertTrue(os.path.exists(p))

    def test_marker_sin_turno_activo_se_consumen(self):
        # T3 in the fixture has no turn.started without completed: no owner
        p = self.marker(support.T3, edad_s=300)
        self.assertNotIn(support.T3, zc_auditor.running_turns())
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertFalse(os.path.exists(p))


class TestFantasmas(BaseAuditor):
    def test_run_turn_sin_registro_alerta(self):
        zc_auditor.auditar(self.cfg(), ahora=time.time(),
                           pids_run_turn=lambda: [support.PID_FALSO],
                           es_run_turn=lambda pid: True)
        self.assertEqual(len(self.sends), 1)
        self.assertIn("🧟", self.sends[0])
        self.assertIn(str(support.PID_FALSO), self.sends[0])

    def test_no_run_turn_o_registrado_no_alerta(self):
        # real LIVE pid: the registry's _prune purges dead pids on read
        p = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(8)"])
        self.addCleanup(p.wait)
        zc_auditor.auditar(self.cfg(), ahora=time.time(),
                           pids_run_turn=lambda: [p.pid],
                           es_run_turn=lambda pid: False)  # not a run-turn
        self.assertEqual(self.sends, [])

        def fn(q):  # same pid registered: legitimate owner
            q = q if isinstance(q, dict) else {}
            q[self.sid] = {"t0": time.time(), "titulo": "t",
                           "ws": "w", "pid": p.pid}
            return q
        self.zt._store("bot-workers.json").update_cross_process(fn, {})
        zc_auditor.auditar(self.cfg(), ahora=time.time(),
                           pids_run_turn=lambda: [p.pid],
                           es_run_turn=lambda pid: True)
        self.assertEqual(self.sends, [])


class TestColaDormida(BaseAuditor):
    def _running(self, sids):
        return support.parchea(self, zc_auditor, "running_turns",
                               lambda max_age=7200: {s: 0 for s in sids})

    def encola_vieja(self, sid, minutos=20):
        zc_workers._queue_put(sid, {"sid": sid, "title": "CV"}, "hola")
        viejo = time.time() - minutos * 60

        def fn(q):
            q = q if isinstance(q, dict) else {}
            q[sid] = [dict(i, ts=viejo) for i in q.get(sid) or []]
            return q
        self.zt._store("bot-queue.json").update_cross_process(fn, {})

    def test_cola_sin_nadie_que_la_tome_alerta(self):
        self._running([])
        self.encola_vieja(self.sid)
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertEqual(len(self.sends), 1)
        self.assertIn("📬", self.sends[0])
        self.assertIn("CV", self.sends[0])

    def test_mac_ocupada_no_alerta_f17_normal(self):
        self._running([self.sid])
        self.encola_vieja(self.sid)
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertEqual(self.sends, [])

    def test_item_legacy_sin_ts_no_alerta(self):
        self._running([])
        self.zt._store("bot-queue.json").update_cross_process(
            lambda q: {self.sid: [{"act": {"title": "CV"},
                                   "text": "sin ts"}]}, {})
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertEqual(self.sends, [])


class TestCachesHuerfanos(BaseAuditor):
    """F51a — CoT cache with vivo=True from a TERMINATED turn with no
    watchdog or worker: the auditor closes it honestly (error recorded,
    taps intact)."""

    T0 = 1760000000

    def setUp(self):
        super().setUp()
        from zcode_bot import razonamiento as rz
        self.rz = rz
        support.parchea(self, zc_auditor, "running_turns",
                        lambda max_age=7200: {})

    def _cache(self, **extra):
        self.rz.guardar(self.sid, self.T0, "cv", ["caja"], ["CoT 1.1"],
                        **extra)

    def _envejecer(self):
        viejo = time.time() - 300
        os.utime(self.rz.cache_path(self.sid, self.T0), (viejo, viejo))

    def _leido(self):
        with open(self.rz.cache_path(self.sid, self.T0)) as f:
            return json.load(f)

    def test_vivo_sin_dueno_se_cierra(self):
        self._cache(vivo=True)
        self._envejecer()
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        d = self._leido()
        self.assertNotIn("vivo", d)
        self.assertTrue(d["finalizado"])
        self.assertEqual(d["error"], "vigilante murió")
        # the boxes remain: the taps keep working
        self.assertEqual(d["cajas"], ["caja"])
        evs = [e for e in zc_audit.leer() if e.get("tipo") == "cot-huerfano"]
        self.assertEqual(len(evs), 1)  # trace in the log…
        self.assertEqual(self.sends, [])  # …no alert to the chat

    def test_fresco_no_se_toca(self):
        self._cache(vivo=True)  # mtime of now: final fetch in flight
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertTrue(self._leido().get("vivo"))

    def test_turno_corriendo_no_se_toca(self):
        self._cache(vivo=True)
        self._envejecer()
        support.parchea(self, zc_auditor, "running_turns",
                        lambda max_age=7200: {self.sid: 0})
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertTrue(self._leido().get("vivo"))

    def test_worker_dueno_no_se_toca(self):
        self._cache(vivo=True)
        self._envejecer()
        support.parchea(self, zc_auditor.zc_workers, "_workers",
                        lambda: {self.sid: {"pid": 1}})
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        self.assertTrue(self._leido().get("vivo"))

    def test_ya_finalizado_no_se_reescribe(self):
        self._cache(finalizado=True)
        self._envejecer()
        zc_auditor.auditar(self.cfg(), ahora=time.time())
        d = self._leido()
        self.assertTrue(d["finalizado"])
        self.assertNotIn("error", d)


class TestThrottleYApagado(BaseAuditor):
    def test_knob_en_cero_apaga(self):
        corrieron = []
        with mock.patch.object(zc_auditor, "_correlacion_fallos",
                               lambda *a, **k: corrieron.append(1)):
            zc_auditor.auditar(self.cfg(auditor_cada_s=0), ahora=100.0)
        self.assertEqual(corrieron, [])

    def test_cadencia_throttlea(self):
        corrieron = []
        with mock.patch.object(zc_auditor, "_correlacion_fallos",
                               lambda *a, **k: corrieron.append(1)):
            cfg = self.cfg(auditor_cada_s=30)
            zc_auditor.auditar(cfg, ahora=100.0)
            zc_auditor.auditar(cfg, ahora=110.0)  # inside the cadence
            self.assertEqual(corrieron, [1])
            zc_auditor.auditar(cfg, ahora=131.0)  # expired: runs again
            self.assertEqual(corrieron, [1, 1])

    def test_invariante_rota_las_otras_corren(self):
        corrieron = []
        with mock.patch.object(zc_auditor, "_correlacion_fallos",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(zc_auditor, "_cola_dormida",
                                  lambda *a, **k: corrieron.append(1)):
            zc_auditor.auditar(self.cfg(), ahora=100.0)  # does not raise
        self.assertEqual(corrieron, [1])


class TestQueueItemYReap(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_queue_item_estampa_ts(self):
        item = zc_workers.QueueItem.from_dict(
            zc_workers.QueueItem({"title": "t"}, "hola", 55.0).to_dict())
        self.assertEqual(item.ts, 55.0)
        self.assertEqual(item.keys(), ("act", "text", "ts"))
        antes = time.time()
        zc_workers._queue_put(support.T2, {"sid": support.T2}, "hola")
        (tomado,) = zc_workers._queue_take(support.T2)
        self.assertGreaterEqual(tomado.ts, antes - 1)

    def test_reapar_hijos_reclama_muertos_y_conserva_vivos(self):
        class _P:
            def __init__(self, vivo):
                self._vivo = vivo
                self.polleos = 0

            def poll(self):
                self.polleos += 1
                return None if self._vivo else 1

        vivos = [_P(True), _P(False)]
        zc_workers._SPAWNED[:] = [_P(True), _P(False), vivos[0]]
        zc_workers._reapar_hijos()
        self.assertEqual(len(zc_workers._SPAWNED), 2)
        self.assertTrue(all(p._vivo for p in zc_workers._SPAWNED))
        self.assertGreaterEqual(vivos[0].polleos, 1)

    def test_http_importa_audit_sin_ciclo_a_core(self):
        # audit is http's failure log: if it pulled from core, the cycle
        # would be systemic (http is imported by everyone)
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "import zcode_bot.utils.http; "
             "assert 'zcode_bot.core' not in sys.modules, 'ciclo http→core'"
             % SRC],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
