"""F33 — espectador: el turno de la Mac streamea al celu por el rollout.

Cobertura: parser de registros (cajas de tools, narración intermedia sí,
respuesta final NO — la manda F32), correlación F20 (turno viejo/compact
ignorados), lectura incremental con offset (línea parcial no se consume,
monstruo se salta), fin por marker consumido, cap duro, knob mac_espectador
y spawn desde on_user_prompt (test_f15). Sin red: sends grabados, wait
fake, reloj de secuencia.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import espectador as esp  # noqa: E402

# El rollout trae startedAt como STRING ISO con Z — el F33 original
# corría los tests con números y el formato real nunca pasó la
# correlación (espectador mudo, incidente 19/10). Los tests usan EL
# FORMATO REAL, no uno convenido.
_T0 = datetime(2026, 10, 19, 15, 0, 0, tzinfo=timezone.utc)
_T0_MS = _T0.timestamp() * 1000.0


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _reg(turn_id="t1", started_at=None, fin=None, text=None, tools=None,
         source="main_turn"):
    return {"querySource": source, "turnId": turn_id,
            "startedAt": started_at or _iso(_T0),
            "response": {"finishReason": fin, "text": text or "",
                         "toolCalls": tools or []}}


def _bash(comando="ls -la"):
    return {"id": "c1", "name": "Bash", "input": {"command": comando}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = dict(self.zt.DEFAULTS)
        self.cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
        self.sends = []
        self.st = {"turn_id": None, "t0_ms": _T0_MS, "paso": 0,
                   "titulo": "proj", "cfg": self.cfg, "avisado": False}
        os.makedirs(os.path.join(self.zt.config.BASE, "cli", "rollout"),
                    exist_ok=True)
        os.makedirs(self.zt.config.STATE_DIR, exist_ok=True)
        self.rollout = esp.rollout_path("s1")

    def _enviar(self):
        return lambda texto: self.sends.append(texto) or 1

    def _escribe(self, *regs):
        with open(self.rollout, "a") as f:
            for r in regs:
                f.write(json.dumps(r) + "\n")


class TestProcesa(_Base):
    """El parser de un registro del rollout → mensajes del chat."""

    def test_tool_call_manda_caja(self):
        n = esp._procesa(_reg(tools=[_bash("ls -la /tmp")]), self.st,
                         self._enviar())
        self.assertEqual(n, 1)
        self.assertIn("espectador", self.sends[0])  # rótulo solo la 1ª vez
        self.assertIn("🔧 <b>Bash</b>", self.sends[1])
        self.assertIn("ls -la /tmp", self.sends[1])
        self.assertIn("· <b>1</b>", self.sends[1])
        self.assertEqual(self.st["turn_id"], "t1")

    def test_texto_intermedio_si_final_no(self):
        esp._procesa(_reg(fin="tool-calls", text="voy a mirar el diff"),
                     self.st, self._enviar())
        self.assertIn("voy a mirar el diff", self.sends[1])
        antes = len(self.sends)
        esp._procesa(_reg(fin="stop", text="respuesta final completa"),
                     self.st, self._enviar())
        # la FINAL no la manda el espectador: la entrega on_stop/F32
        self.assertEqual(len(self.sends), antes)
        self.assertNotIn("respuesta final completa", "".join(self.sends))

    def test_turno_viejo_ignorado(self):
        n = esp._procesa(_reg(turn_id="viejo",
                              started_at=_iso(datetime(
                                  2026, 10, 19, 14, 0, 0,
                                  tzinfo=timezone.utc)),
                              tools=[_bash()]), self.st, self._enviar())
        self.assertEqual((n, self.sends, self.st["turn_id"]), (0, [], None))

    def test_started_at_iso_string_se_correlaciona(self):
        """EL bug del F33 original: startedAt viene ISO con Z (formato
        real del rollout) — con isinstance((int, float)) la correlación
        moría y el espectador no mandaba NADA (incidente 19/10)."""
        n = esp._procesa(_reg(tools=[_bash()]), self.st, self._enviar())
        self.assertEqual(n, 1)
        self.assertEqual(self.st["turn_id"], "t1")
        self.assertIn("🔧 <b>Bash</b>", self.sends[1])

    def test_started_at_roto_o_ausente_no_correlaciona(self):
        self.assertEqual(esp._procesa(
            _reg(started_at="no-es-una-fecha", tools=[_bash()]),
            self.st, self._enviar()), 0)
        self.assertEqual(self.st["turn_id"], None)
        self.assertEqual(esp._ts_ms(None), None)
        self.assertEqual(esp._ts_ms(""), None)

    def test_started_at_epoch_numerico_tambien_vale(self):
        n = esp._procesa(_reg(started_at=_T0_MS, tools=[_bash()]),
                         self.st, self._enviar())
        self.assertEqual(n, 1)
        self.assertEqual(self.st["turn_id"], "t1")

    def test_correlacion_fija_y_filtra_otros(self):
        esp._procesa(_reg(turn_id="t1"), self.st, self._enviar())
        self.assertEqual(self.st["turn_id"], "t1")
        antes = len(self.sends)
        esp._procesa(_reg(turn_id="t2", tools=[_bash()]), self.st,
                     self._enviar())
        self.assertEqual(len(self.sends), antes)  # turno cruzado: fuera

    def test_compact_ignorado(self):
        n = esp._procesa(_reg(source="compact", tools=[_bash()]),
                         self.st, self._enviar())
        self.assertEqual((n, self.sends), (0, []))

    def test_tools_stream_apagado(self):
        self.cfg["tools_stream"] = False
        esp._procesa(_reg(fin="tool-calls", text="analizo",
                          tools=[_bash()]), self.st, self._enviar())
        self.assertEqual(len(self.sends), 2)  # rótulo + narración, sin caja
        self.assertNotIn("🔧", "".join(self.sends))

    def test_caja_preset_minimo_filtra(self):
        self.cfg["tools_caja_filtro"] = "minimo"
        esp._procesa(_reg(tools=[{"id": "c", "name": "Grep",
                                  "input": {"pattern": "x"}}]),
                     self.st, self._enviar())
        self.assertEqual(self.sends, [])  # Grep no hace nada: sin caja


class TestLeerNuevos(_Base):
    """El tail incremental: offset, línea parcial, registro monstruo."""

    def test_linea_parcial_no_se_consumio(self):
        self._escribe(_reg(turn_id="a"))
        with open(self.rollout, "a") as f:
            f.write(json.dumps(_reg(turn_id="b"))[:20])  # a mitad
        regs, pos = esp._leer_nuevos(self.rollout, 0)
        self.assertEqual([r["turnId"] for r in regs], ["a"])
        regs2, pos2 = esp._leer_nuevos(self.rollout, pos)
        self.assertEqual(regs2, [])
        with open(self.rollout, "a") as f:
            f.write(json.dumps(_reg(turn_id="b"))[20:] + "\n")
        regs3, _ = esp._leer_nuevos(self.rollout, pos)
        self.assertEqual([r["turnId"] for r in regs3], ["b"])

    def test_registro_monstruo_salta_y_avanza(self):
        linea = "x" * 5000 + "\n"
        with open(self.rollout, "w") as f:
            f.write(json.dumps(_reg(turn_id="ok")) + "\n" + linea)
        with mock.patch.object(esp, "REG_CAP", 200):
            regs, pos = esp._leer_nuevos(self.rollout, 0)
        self.assertEqual([r["turnId"] for r in regs], ["ok"])
        self.assertEqual(pos, os.path.getsize(self.rollout))

    def test_sin_novedades_y_archivo_ausente(self):
        self.assertEqual(esp._leer_nuevos(self.rollout, 0), ([], 0))
        self._escribe(_reg())
        regs, pos = esp._leer_nuevos(self.rollout, 0)
        self.assertEqual(len(regs), 1)
        self.assertEqual(esp._leer_nuevos(self.rollout, pos), ([], pos))


class TestMirar(_Base):
    """El loop: fin por marker, cap duro, rótulo una sola vez."""

    def _deps(self, marker=True, waits=None, tiempos=None):
        d = {"send": lambda cfg, t, loud=None: self.sends.append(t) or True,
             "sleep": lambda s: None,
             "now": self._reloj(tiempos or [0.0] * 12),
             "marker_vivo": lambda: marker if isinstance(marker, bool)
             else marker(),
             "wait": waits if waits is not None
             else (lambda paths, timeout: False)}
        return d

    def _reloj(self, secuencia):
        estado = {"i": 0}

        def f():
            t = secuencia[min(estado["i"], len(secuencia) - 1)]
            estado["i"] += 1
            return t
        return f

    def test_fin_por_marker_consumido(self):
        pasos = esp.mirar("s1", "/fake/proj", self.cfg,
                          self._deps(marker=False))
        self.assertEqual(pasos, 0)
        self.assertEqual(self.sends, [])  # sin turno: ni el rótulo

    def test_cap_duro_corta(self):
        pasos = esp.mirar("s1", "/fake/proj", self.cfg, self._deps(
            tiempos=[0.0, 0.0, esp.CAP_S + 1.0, esp.CAP_S + 1.0,
                     esp.CAP_S + 1.0]))
        self.assertEqual(pasos, 0)

    def test_flujo_marcador_y_rotulo_una_vez(self):
        estado = {"vueltas": 0}

        def marker():
            estado["vueltas"] += 1
            return estado["vueltas"] < 3

        def wait(paths, timeout):
            if estado["vueltas"] == 1:
                self._escribe(_reg(tools=[_bash("git status")]))
            return False

        pasos = esp.mirar("s1", "/fake/proj", self.cfg,
                          self._deps(marker=marker, waits=wait))
        self.assertEqual(pasos, 1)
        self.assertEqual(self.sends[0].count("espectador"), 1)
        self.assertEqual(len([s for s in self.sends if "🔧" in s]), 1)


class TestKnobYSpawn(_Base):
    """mac_espectador apaga el feature entero (config knob)."""

    def test_knob_default_encendido(self):
        self.assertTrue(self.zt.DEFAULTS.get("mac_espectador"))

    def test_marker_y_rollout_paths(self):
        self.assertEqual(esp.marker_path("s1"),
                         self.zt.marker_path("s1"))  # misma fórmula (hooks)


if __name__ == "__main__":
    unittest.main()
