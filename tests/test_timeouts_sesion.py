# -*- coding: utf-8 -*-
"""F29 — adaptive session timeouts (incident 18:10 on 15/10): session
requests (resume/setModel/setMode) return the COMPLETE history (~30s
per request in a large session under load) and with a FIXED 30/60s
timeout they expired → the turn died silently and IN A LOOP (mode_set
only registers after a successful _proto_ready, so the next turn paid
everything again; the user's 4 /compact attempts failed on the same
setModel). Now the ceiling scales with the transcript (60s + 4s/MB,
cap 300) and session/compact uses Damian's rule calibrated with real
compacts (135k tokens → 103s ≈ 0.76 s/ktok): 1.2 s/1k tokens, base 300,
cap 600.

F31 (16/10) — the context figure is read from the rollout with a
growing window (one record weighs as much as the context: with fixed
64KB the parse failed on large sessions) and /ctx shows that number
instead of the session/resume probe (contextUsed=0 always), notifying
when a session was compacted with no subsequent turn."""
import datetime
import json
import os
import unittest
from unittest import mock

import support

from zcode_bot import config as zc_config
from zcode_bot import core as zc_core
from zcode_bot import reporting as zc_reporting


def _jsonl_de(sid, entradas):
    """Fake model-io jsonl: each entry is a dict with response."""
    d = os.path.join(zc_config.BASE, "cli", "rollout")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "model-io-%s.jsonl" % sid)
    with open(p, "w") as f:
        for e in entradas:
            f.write(json.dumps(e) + "\n")
    return p


class TestTimeoutPorMb(unittest.TestCase):
    """60s base + 4s/MB, cap 300 — a ceiling, not a wait."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_escalas_y_tope(self):
        casos = {0: 60, 5: 80, 25: 160, 60: 300, 100: 300}
        for mb, esperado in casos.items():
            with self.subTest(mb=mb), \
                    mock.patch.object(zc_core, "transcript_mb",
                                      return_value=float(mb)):
                self.assertEqual(zc_core._timeout_por_mb("s"), esperado)

    def test_sesion_nueva_sin_archivo(self):
        # no model-io jsonl → base 60 (the usual default for /task new)
        self.assertEqual(zc_core._timeout_por_mb("sess_INEXISTENTE"), 60)


class TestContextoKtokens(unittest.TestCase):
    """REAL context tokens: the last usage in the jsonl."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sid = support.T2

    def test_lee_la_ultima_usage_canonica(self):
        _jsonl_de(self.sid, [
            {"response": {"usage": {"inputTokens": 90000}}},
            {"response": {"usage": {"inputTokens": 146706,
                                    "cacheReadTokens": 146048}}},
        ])
        self.assertAlmostEqual(zc_core._contexto_ktokens(self.sid), 146.706)

    def test_fallback_provider_snake_case(self):
        _jsonl_de(self.sid, [
            {"response": {"providerMetadata": {"anthropic": {"usage": {
                "input_tokens": 650,
                "cache_read_input_tokens": 146048}}}}},
        ])
        self.assertAlmostEqual(zc_core._contexto_ktokens(self.sid), 146.698)

    def test_sin_usage_o_sin_archivo_da_none(self):
        _jsonl_de(self.sid, [{"response": {"body": "sin usage"}}])
        self.assertIsNone(zc_core._contexto_ktokens(self.sid))
        self.assertIsNone(zc_core._contexto_ktokens("sess_INEXISTENTE"))
        # broken jsonl: never raises
        p = _jsonl_de(self.sid, [])
        with open(p, "a") as f:
            f.write("{trucha\n")
        self.assertIsNone(zc_core._contexto_ktokens(self.sid))

    def test_registro_mas_grande_que_la_cola_de_64kb(self):
        # each model-io record weighs as much as the whole context: at
        # 200KB+ the fixed 64KB queue only saw a truncated JSON → None
        # (16/10: a 180k-token session, _timeout_compact fell to base)
        relleno = "x" * (300 * 1024)
        _jsonl_de(self.sid, [
            {"request": {"messages": relleno},
             "response": {"usage": {"inputTokens": 5000}}},
            {"request": {"messages": relleno},
             "response": {"usage": {"inputTokens": 180626}}},
        ])
        self.assertAlmostEqual(zc_core._contexto_ktokens(self.sid), 180.626)

    def test_ultimo_registro_sin_usage_usa_el_anterior(self):
        _jsonl_de(self.sid, [
            {"response": {"usage": {"inputTokens": 7000}}},
            {"response": {"error": "nada"}},
        ])
        self.assertAlmostEqual(zc_core._contexto_ktokens(self.sid), 7.0)


class TestUltimoUso(unittest.TestCase):
    """(ktokens, epoch, source) of the last request with usage."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.sid = support.T2

    def test_tupla_completa(self):
        _jsonl_de(self.sid, [
            {"completedAt": "2026-10-16T02:03:33.209Z",
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 180626}}},
            {"completedAt": "2026-10-16T14:28:23.507Z",
             "querySource": "main_turn",
             "response": {"usage": {"inputTokens": 45625,
                                    "cacheReadTokens": 2176}}},
        ])
        ktok, epoch, fuente = zc_core.ultimo_uso(self.sid)
        self.assertAlmostEqual(ktok, 45.625)
        esperado = datetime.datetime(
            2026, 10, 16, 14, 28, 23, 507000,
            tzinfo=datetime.timezone.utc).timestamp()
        self.assertAlmostEqual(epoch, esperado)
        self.assertEqual(fuente, "main_turn")

    def test_sin_completed_at_ni_fuente(self):
        _jsonl_de(self.sid, [{"response": {"usage": {"inputTokens": 900}}}])
        self.assertEqual(zc_core.ultimo_uso(self.sid), (0.9, 0.0, ""))

    def test_completed_at_roto_no_lanza(self):
        _jsonl_de(self.sid, [{"completedAt": "ayer", "querySource": "compact",
                              "response": {"usage": {"inputTokens": 900}}}])
        self.assertEqual(zc_core.ultimo_uso(self.sid), (0.9, 0.0, "compact"))

    def test_sin_archivo(self):
        self.assertIsNone(zc_core.ultimo_uso("sess_INEXISTENTE"))


class TestContextReport(unittest.TestCase):
    """/ctx (F31): real number from the rollout; honest post-compact notice."""

    COMPACT = 1792115800.0  # epoch of task 2's compact.completed

    def setUp(self):
        self.zt = support.fresh(self)
        tasks = [
            {"n": 1, "sid": support.T1, "ws": "/ws/bot", "ctx": 185000,
             "when": int((self.COMPACT + 3600) * 1000), "model": "GLM-5.3",
             "title": "testing"},
            {"n": 2, "sid": support.T2, "ws": "/ws/cv", "ctx": 50473,
             "when": int((self.COMPACT - 120) * 1000), "model": "GLM-5.3",
             "title": "CV"},
            {"n": 3, "sid": support.T3, "ws": "/ws/bot", "ctx": 68719,
             "when": int((self.COMPACT - 9000) * 1000), "model": "GLM-5.3",
             "title": "MCP"},
        ]
        patches = [
            mock.patch.object(zc_reporting, "_ctx_tasks", return_value=tasks),
            mock.patch.object(zc_reporting, "_compactiones",
                              return_value={support.T1: self.COMPACT - 7200,
                                            support.T2: self.COMPACT}),
            mock.patch.object(zc_reporting, "active_workspace_path",
                              return_value="/ws/bot"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # T1: compacted 2h ago and WITH a later request → real number
        _jsonl_de(support.T1, [
            {"completedAt": support._iso(self.COMPACT - 7300),
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 185000}}},
            {"completedAt": support._iso(self.COMPACT + 3600),
             "querySource": "main_turn",
             "response": {"usage": {"inputTokens": 23227}}},
        ])
        # T2: the latest on disk is the compact itself → no later turn
        _jsonl_de(support.T2, [
            {"completedAt": support._iso(self.COMPACT - 1),
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 41112}}},
        ])
        # T3: no rollout or compact → falls back to the tasks-repo number

    def test_numero_real_tras_el_compact(self):
        lineas = zc_reporting.context_report().splitlines()
        self.assertIn("1. 🟢 23.2K (2%) — testing", lineas)
        self.assertFalse(any("185" in l for l in lineas),
                         "el número pre-compact no debe aparecer")

    def test_compactada_sin_turno_posterior_avisa(self):
        hc = datetime.datetime.fromtimestamp(self.COMPACT).strftime("%H:%M")
        lineas = zc_reporting.context_report().splitlines()
        self.assertIn("2. 🧹 compactada %s — el tamaño real se ve en el"
                      " próximo turno (antes 50.5K) · 📁cv — CV" % hc, lineas)

    def test_sin_rollout_cae_al_repo(self):
        lineas = zc_reporting.context_report().splitlines()
        self.assertIn("3. 🟢 68.7K (7%) — MCP", lineas)


class TestTimeoutCompact(unittest.TestCase):
    """Damian's rule: 1.2 s/1k tokens; base 300, cap 600."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_regla_lineal(self):
        casos = {None: 300,   # no data → the usual default
                 0: 300,      # (base dominates)
                 135: 300,    # 162 by rule, but the 300 base dominates
                 300: 360,    # Damian's exact number (300k → 6 min)
                 600: 600,    # cap
                 900: 600}
        for ktok, esperado in casos.items():
            with self.subTest(ktok=ktok), \
                    mock.patch.object(zc_core, "_contexto_ktokens",
                                      return_value=ktok):
                self.assertEqual(zc_core._timeout_compact("s"), esperado)


class _FakeCTimeos:
    """Fake ProtoClient that RECORDS the timeout of each request."""

    def __init__(self):
        self.vistos = []  # (method, timeout)
        self.closed = False

    def request(self, method, params=None, timeout=None):
        self.vistos.append((method, timeout))
        return {"result": {}}

    def close(self):
        self.closed = True


class TestProtoReadyTimeos(unittest.TestCase):
    """_proto_ready uses the SAME per-MB ceiling for resume/setModel/
    setMode — all three return the complete history (incident 18:10)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.c = _FakeCTimeos()

    def test_los_tres_requests_con_el_techo_del_mb(self):
        with mock.patch.object(zc_core, "transcript_mb", return_value=25.0):
            err = zc_core._proto_ready(self.c, support.T2,
                                       skip_model=False, skip_mode=False)
        self.assertIsNone(err)
        esperado = 60 + int(25.0 * 4)  # 160
        self.assertEqual(self.c.vistos, [
            ("session/resume", esperado),
            ("session/setModel", esperado),
            ("session/setMode", esperado)])

    def test_error_del_request_se_propaga_como_mensaje(self):
        c = _FakeCTimeos()
        c.request = lambda method, params=None, timeout=None: (
            {"error": {"code": -2, "message": "timeout session/resume"}}
            if method == "session/resume" else {"result": {}})
        err = zc_core._proto_ready(c, support.T2)
        self.assertIn("❌ resume:", err)
        self.assertIn("timeout", err)


class TestCompactTimeos(unittest.TestCase):
    """/compact: resume+setModel with the per-MB ceiling and compact with
    the per-token ceiling — the user's entire flow (4 failed /compact
    attempts today)."""

    def test_timeouts_del_flujo_completo(self):
        self.zt = support.fresh(self)
        c = _FakeCTimeos()
        esperas = []  # timeouts used to wait for the end event
        patches = [
            mock.patch.object(zc_reporting, "_ctx_tasks", lambda n=30: [
                {"sid": support.T2, "ws": support.PROJ_CV, "title": "CV"}]),
            mock.patch.object(zc_reporting.zc_proto, "make",
                              lambda cwd=None, **kw: c),
            mock.patch.object(zc_reporting, "_model_snapshot",
                              lambda sid: None),
            mock.patch.object(zc_reporting, "_wait_cli_event",
                              lambda sid, t0, evs, timeout:
                              (esperas.append(timeout),
                               {"event": "compact.completed",
                                "durationMs": 90000})[1]),
            mock.patch.object(zc_reporting, "_live_skip_clear",
                              lambda sid: None),
            mock.patch.object(zc_core, "transcript_mb", return_value=25.0),
            mock.patch.object(zc_core, "_contexto_ktokens",
                              return_value=300.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = zc_reporting.zcode_compact(sid=support.T2)
        self.assertIn("Compactación de «CV» lista", resp)
        t_mb = 60 + int(25.0 * 4)          # 160 — resume and setModel
        t_comp = min(600, max(300, int(300 * 1.2)))  # 360 — the compact
        self.assertEqual(c.vistos, [
            ("session/list", 30),
            ("session/resume", t_mb),
            ("session/setModel", t_mb),
            ("session/compact", t_comp),
            ("session/messages", 30)])
        # the end event is awaited with the compact budget +30
        self.assertEqual(esperas, [t_comp + 30])

    def test_ack_temprano_llego_tu_eleccion(self):
        """F35 — Damian's request: two feedbacks on a button tap.
        Telegram's native 👀 (answerCallbackQuery) covers the first;
        this is the app's one: as soon as session/list confirms idle,
        «📬 Llegó tu elección» goes out BEFORE the heavy resume and the
        final report — the phone no longer stays mute for the minutes
        the compaction takes."""
        self.zt = support.fresh(self)
        c = _FakeCTimeos()
        sends = []
        patches = [
            mock.patch.object(zc_reporting, "_ctx_tasks", lambda n=30: [
                {"sid": support.T2, "ws": support.PROJ_CV, "title": "CV"}]),
            mock.patch.object(zc_reporting.zc_proto, "make",
                              lambda cwd=None, **kw: c),
            mock.patch.object(zc_reporting, "_model_snapshot",
                              lambda sid: None),
            mock.patch.object(zc_reporting, "_wait_cli_event",
                              lambda sid, t0, evs, timeout:
                              {"event": "compact.completed",
                               "durationMs": 90000}),
            mock.patch.object(zc_reporting, "_live_skip_clear",
                              lambda sid: None),
            mock.patch.object(zc_core, "transcript_mb", return_value=25.0),
            mock.patch.object(zc_core, "_contexto_ktokens",
                              return_value=300.0),
            mock.patch.object(zc_reporting, "load_config",
                              lambda: {"token": "T", "chat_id": 1}),
            mock.patch.object(zc_reporting.uhttp, "send",
                              lambda cfg, text, loud=None, markup=None:
                                  (sends.append(text), True)[1]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        resp = zc_reporting.zcode_compact(sid=support.T2)
        # the final report is RETURNed (the dispatcher sends it) and the
        # ack was the flow's ONLY send: zero silence, no noise either
        self.assertIn("Compactación de «CV» lista", resp)
        self.assertEqual(len(sends), 1)
        self.assertIn("Llegó tu elección", sends[0])
        self.assertIn("«CV»", sends[0])


if __name__ == "__main__":
    unittest.main()
