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
1.2 s/1k tokens, base 300, tope 600."""
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
