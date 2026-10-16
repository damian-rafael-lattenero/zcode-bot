# -*- coding: utf-8 -*-
"""F29 — timeouts adaptativos de sesión (incidente 18:10 del 15/10):
los requests de sesión (resume/setModel/setMode) devuelven el historial
COMPLETO (~30s por request en una sesión grande bajo carga) y con timeout
FIJO de 30/60s vencían → el turno moría en silencio y EN BUCLE (el
mode_set recién se registra tras un _proto_ready exitoso, así que el
próximo turno volvía a pagar todo; los 4 /compact del usuario fallaban
por el mismo setModel). Ahora el techo escala con el transcripto
(60s + 4s/MB, tope 300) y session/compact usa la regla de Damian
calibrada con compacts reales (135k tokens → 103s ≈ 0.76 s/ktok):
1.2 s/1k tokens, base 300, tope 600.

F31 (16/10) — el dato de contexto se lee del rollout con ventana
creciente (un registro pesa lo que el contexto: con 64KB fijos el parse
fallaba en las sesiones grandes) y /ctx muestra ese número en vez del
probe de session/resume (contextUsed=0 siempre), avisando cuando una
sesión fue compactada sin turno posterior."""
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
    """model-io jsonl de mentira: cada entrada es un dict con response."""
    d = os.path.join(zc_config.BASE, "cli", "rollout")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "model-io-%s.jsonl" % sid)
    with open(p, "w") as f:
        for e in entradas:
            f.write(json.dumps(e) + "\n")
    return p


class TestTimeoutPorMb(unittest.TestCase):
    """60s base + 4s/MB, tope 300 — el techo, no una espera."""

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
        # sin model-io jsonl → base 60 (el default de siempre para /task new)
        self.assertEqual(zc_core._timeout_por_mb("sess_INEXISTENTE"), 60)


class TestContextoKtokens(unittest.TestCase):
    """Tokens REALES del contexto: la última usage del jsonl."""

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
        # jsonl roto: nunca lanza
        p = _jsonl_de(self.sid, [])
        with open(p, "a") as f:
            f.write("{trucha\n")
        self.assertIsNone(zc_core._contexto_ktokens(self.sid))

    def test_registro_mas_grande_que_la_cola_de_64kb(self):
        # cada registro del model-io pesa lo que el contexto entero: con
        # 200KB+ la cola fija de 64KB solo veía un JSON truncado → None
        # (16/10: sesión de 180k tokens, _timeout_compact caía al base)
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
    """(ktokens, epoch, fuente) del último request con usage."""

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
    """/ctx (F31): número real del rollout; aviso honesto post-compact."""

    COMPACT = 1792115800.0  # epoch del compact.completed de la task 2

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
        # T1: compactada hace 2h y CON request posterior → número real
        _jsonl_de(support.T1, [
            {"completedAt": support._iso(self.COMPACT - 7300),
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 185000}}},
            {"completedAt": support._iso(self.COMPACT + 3600),
             "querySource": "main_turn",
             "response": {"usage": {"inputTokens": 23227}}},
        ])
        # T2: lo último en disco es el propio compact → sin turno posterior
        _jsonl_de(support.T2, [
            {"completedAt": support._iso(self.COMPACT - 1),
             "querySource": "compact",
             "response": {"usage": {"inputTokens": 41112}}},
        ])
        # T3: sin rollout ni compact → cae al número del repo de tasks

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
    """Regla de Damian: 1.2 s/1k tokens; base 300, tope 600."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_regla_lineal(self):
        casos = {None: 300,   # sin datos → el default de siempre
                 0: 300,      # (base domina)
                 135: 300,    # 162 por regla, pero la base 300 domina
                 300: 360,    # el número exacto de Damian (300k → 6 min)
                 600: 600,    # tope
                 900: 600}
        for ktok, esperado in casos.items():
            with self.subTest(ktok=ktok), \
                    mock.patch.object(zc_core, "_contexto_ktokens",
                                      return_value=ktok):
                self.assertEqual(zc_core._timeout_compact("s"), esperado)


class _FakeCTimeos:
    """ProtoClient de mentira que GRABA el timeout de cada request."""

    def __init__(self):
        self.vistos = []  # (method, timeout)
        self.closed = False

    def request(self, method, params=None, timeout=None):
        self.vistos.append((method, timeout))
        return {"result": {}}

    def close(self):
        self.closed = True


class TestProtoReadyTimeos(unittest.TestCase):
    """_proto_ready usa el MISMO techo por MB en resume/setModel/setMode —
    los tres devuelven el historial completo (incidente 18:10)."""

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
    """/compact: resume+setModel con techo por MB y compact con techo por
    tokens — el flujo entero del usuario (4 /compact fallados hoy)."""

    def test_timeouts_del_flujo_completo(self):
        self.zt = support.fresh(self)
        c = _FakeCTimeos()
        esperas = []  # timeouts con que se esperó el evento de fin
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
        t_mb = 60 + int(25.0 * 4)          # 160 — resume y setModel
        t_comp = min(600, max(300, int(300 * 1.2)))  # 360 — el compact
        self.assertEqual(c.vistos, [
            ("session/list", 30),
            ("session/resume", t_mb),
            ("session/setModel", t_mb),
            ("session/compact", t_comp),
            ("session/messages", 30)])
        # el evento de fin se espera con el presupuesto del compact +30
        self.assertEqual(esperas, [t_comp + 30])


if __name__ == "__main__":
    unittest.main()
