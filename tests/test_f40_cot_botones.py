"""F40/F43/F46 — captura al cierre con CAJAS por tamaño (fallback del vivo).

El modelo de Damian (F43): cajas que se sellan al llegar al tope
(COT_CAJA_CAP), botón por caja. F46: rótulos «CoT R.C» — razonada R,
caja C dentro de ella (nunca dos «CoT 1»). capturar() es el fallback
cuando el vigilante en vivo no pudo nacer.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock  # noqa: F401

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot.commands import handlers_info as hi  # noqa: E402
from zcode_bot.updates import _cb_text  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
T0 = 1760000000
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"


def _msg(rol, *parts):
    return {"info": {"role": rol}, "parts": list(parts)}


def _r(texto):
    return {"type": "reasoning", "text": texto}


HISTORIAL = [
    _msg("user", {"type": "text", "text": "primero"}),
    _msg("assistant", _r("razón vieja A"), {"type": "tool", "tool": "Read"}),
    _msg("assistant", _r("razón vieja B"), {"type": "text", "text": "ok"}),
    _msg("user", {"type": "text", "text": "segundo"}),
    _msg("assistant", _r("razón 1 del turno"), {"type": "step-finish"}),
    _msg("assistant", {"type": "tool", "tool": "Edit"}, _r("razón 2 del turno")),
    _msg("assistant", {"type": "reasoning", "text": "   "}),  # vacía: se tira
    _msg("assistant", {"type": "text", "text": "respuesta final"}),
]


class TestCajasDelTurno(unittest.TestCase):
    def test_dos_razonadas_rotulos_razonada_caja(self):
        """La tool cierra la 1ª razonada; cada una llena UNA caja — F46:
        «CoT 1.1» y «CoT 2.1», distintos (nunca dos «CoT 1»)."""
        cajas, rotulos = rz.cajas_del_turno(HISTORIAL)
        self.assertEqual(cajas, ["razón 1 del turno", "razón 2 del turno"])
        self.assertEqual(rotulos, ["CoT 1.1", "CoT 2.1"])

    def test_razonada_larga_se_llena_en_cajas_del_tope_mas_cola(self):
        msgs = [_msg("user", {"type": "text", "text": "p"}),
                _msg("assistant", _r("z" * (rz.COT_CAJA_CAP * 2 + 500)),
                     {"type": "text", "text": "fin"})]
        cajas, rotulos = rz.cajas_del_turno(msgs)
        self.assertEqual([len(c) for c in cajas],
                         [rz.COT_CAJA_CAP, rz.COT_CAJA_CAP, 500])
        self.assertEqual(rotulos, ["CoT 1.1", "CoT 1.2", "CoT 1.3"])

    def test_reasonings_contiguos_son_la_misma_razonada(self):
        msgs = [_msg("user", {"type": "text", "text": "p"}),
                _msg("assistant", _r("a"), _r("b")),
                _msg("assistant", _r("c"), {"type": "text", "text": "x"})]
        cajas, rotulos = rz.cajas_del_turno(msgs)
        self.assertEqual(cajas, ["abc"])
        self.assertEqual(rotulos, ["CoT 1.1"])

    def test_sin_reasoning(self):
        cajas, rotulos = rz.cajas_del_turno([HISTORIAL[0], HISTORIAL[-1]])
        self.assertEqual((cajas, rotulos), ([], []))


class TestSellador(unittest.TestCase):
    """El corazón de F43: sella al tope, la cola cierra, reinicia."""

    def test_sella_a_medida_que_se_llena(self):
        s = rz.SelladorCot(cap=100)
        self.assertEqual(s.alimentar("x" * 60), [])          # 60: nada
        nuevas = s.alimentar("y" * 60)                        # 120 → caja 1
        self.assertEqual([(c, n) for c, n in nuevas],
                         [("x" * 60 + "y" * 40, 1)])
        # 320 total: el mismo delta tapa las cajas 2 y 3 JUNTAS
        nuevas = s.alimentar("z" * 200)
        self.assertEqual([n for _c, n in nuevas], [2, 3])
        self.assertEqual(s.alimentar("w" * 10), [])  # 330 < 400: nada

    def test_cerrar_sella_la_cola_y_reinicia(self):
        s = rz.SelladorCot(cap=100)
        s.alimentar("a" * 150)  # caja 1 sellada, cola de 50
        self.assertEqual(s.cerrar(), [("a" * 50, 2)])          # cola = caja 2
        self.assertEqual(s.cerrar(), [])                       # ya vacío
        nuevas = s.alimentar("b" * 120)                        # REINICIÓ: 1
        self.assertEqual([n for _c, n in nuevas], [1])

    def test_en_curso(self):
        s = rz.SelladorCot(cap=100)
        s.alimentar("a" * 250)  # 2 selladas, 50 en curso → caja 3
        self.assertEqual(s.en_curso, (50, 3))


class _FakeProto:
    def __init__(self, msgs, resume_error=None):
        self.msgs, self.resume_error, self.vistos, self.cerrado = msgs, resume_error, [], False

    def request(self, method, params=None, timeout=None):
        self.vistos.append(method)
        if method == "session/resume":
            return ({"error": {"code": -1, "message": self.resume_error}}
                    if self.resume_error else {"result": {}})
        if method == "session/messages":
            return {"result": {"messages": self.msgs}}
        return {"result": {}}

    def close(self):
        self.cerrado = True


class TestCapturar(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends = []
        support.parchea(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), True)[1])

    def _make(self, fake):
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: fake)

    def test_cachea_y_manda_un_boton_por_caja(self):
        fake = _FakeProto(HISTORIAL)
        self._make(fake)
        n = rz.capturar(CFG, SID, "cv", T0, "/w")
        self.assertEqual(n, 2)
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["cajas"], ["razón 1 del turno", "razón 2 del turno"])
        self.assertEqual(d["rotulos"], ["CoT 1.1", "CoT 2.1"])
        texto, markup = self.sends[0]
        self.assertIn("2 cajas", texto)
        botones = [b for f in markup["inline_keyboard"] for b in f]
        # dos razonadas → «CoT 1.1» y «CoT 2.1»: distintos, índice global
        self.assertEqual([b["text"] for b in botones], ["CoT 1.1", "CoT 2.1"])
        self.assertEqual(botones[1]["callback_data"], "ct:%s:%d:2" % (SID, T0))
        for b in botones:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_sin_razonamiento_avisa_en_una_linea(self):
        """F45: el turno cerró sin razonar → 0 cajas y UNA línea «sin CoT»
        (sin teclado, sin cache) — antes el silencio era indistinguible de
        un bug. Con el knob apagado, el silencio de siempre."""
        self._make(_FakeProto([HISTORIAL[0], HISTORIAL[-1]]))
        self.assertEqual(rz.capturar(dict(CFG, cot_aviso_vacio=True), SID,
                                     "cv", T0, "/w"), 0)
        self.assertEqual(len(self.sends), 1)
        texto, markup = self.sends[0]
        self.assertIn("sin CoT", texto)
        self.assertIn("«cv»", texto)
        self.assertIsNone(markup)
        self.assertFalse(os.path.exists(rz.cache_path(SID, T0)))
        self.sends.clear()
        self._make(_FakeProto([HISTORIAL[0], HISTORIAL[-1]]))
        self.assertEqual(rz.capturar(CFG, SID, "cv", T0, "/w"), 0)
        self.assertEqual(self.sends, [])

    def test_sin_historial_no_manda_nada(self):
        self._make(_FakeProto([]))
        self.assertIsNone(rz.capturar(dict(CFG, cot_aviso_vacio=True), SID,
                                      "cv", T0, "/w"))
        self.assertEqual(self.sends, [])

    def test_resume_fallido_no_rompe(self):
        self._make(_FakeProto(HISTORIAL, resume_error="no such session"))
        self.assertIsNone(rz.capturar(CFG, SID, "cv", T0, "/w"))
        self.assertEqual(self.sends, [])

    def test_mas_cajas_que_botones_capa_el_teclado(self):
        gigante = "l" * (rz.COT_CAJA_CAP * 22)  # una razonada → 22 cajas
        msgs = [HISTORIAL[3],
                _msg("assistant", _r(gigante), {"type": "text", "text": "x"})]
        self._make(_FakeProto(msgs))
        self.assertEqual(rz.capturar(CFG, SID, "cv", T0, "/w"), 22)
        texto, markup = self.sends[0]
        self.assertIn("22 cajas", texto)
        self.assertIn("primeras %d" % rz.COT_BOTONES_MAX, texto)
        plano = [b for f in markup["inline_keyboard"] for b in f]
        self.assertEqual(len(plano), rz.COT_BOTONES_MAX)
        self.assertEqual([len(f) for f in markup["inline_keyboard"]], [5] * 4)


class TestMostrar(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _cache(self, cajas, rotulos):
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                       "cajas": cajas, "rotulos": rotulos}, f)

    def test_caja_chica_entera_con_su_rotulo(self):
        self._cache(["pienso <b>esto</b>", "y esto"], ["CoT 1.1", "CoT 2.1"])
        out = rz.mostrar(SID, T0, 1)
        self.assertIsInstance(out, str)
        self.assertIn("🧠 <b>CoT 1.1</b> · «cv»", out)
        self.assertIn("<pre>pienso &lt;b&gt;esto&lt;/b&gt;</pre>", out)

    def test_caja_vieja_sin_rotulos_y_grande_en_partes(self):
        # compat: cache F40 viejo (bloques, sin rotulos) + caja gigante
        grande = "".join("línea de razonamiento %04d\n" % i for i in range(400))
        with open(rz.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                       "bloques": [grande]}, f)
        texto, markup = rz.mostrar(SID, T0, 1)
        self.assertIn("🧠 <b>CoT 1</b> · «cv»", texto)
        botones = [b for f in markup["inline_keyboard"] for b in f]
        self.assertEqual(botones[0]["text"], "[1/%d]" % len(botones))
        self.assertEqual(botones[1]["callback_data"],
                         "ct:%s:%d:1:2" % (SID, T0))
        t2, _ = rz.mostrar(SID, T0, 1, 2)
        self.assertIn("parte 2/%d" % len(botones), t2)

    def test_fuera_de_rango_y_sin_cache(self):
        self._cache(["uno"], ["CoT 1"])
        self.assertIn("Caja fuera de rango (1 a 1)", rz.mostrar(SID, T0, 5))
        self.assertIn("ya no está guardado", rz.mostrar(SID, T0 + 1, 1))

    def test_cmd_cotb_parsea_el_boton(self):
        self._cache(["uno", "dos"], ["CoT 1.1", "CoT 2.1"])
        texto = _cb_text("ct:%s:%d:2" % (SID, T0))
        self.assertEqual(texto, "/cotb %s %d 2" % (SID, T0))
        out = hi._cmd_cotb(CFG, texto.split(" ", 1)[1])
        self.assertIn("🧠 <b>CoT 2.1</b>", out)  # el rótulo de SU razonada
        self.assertIn("<pre>dos</pre>", out)
        self.assertIn("Uso:", hi._cmd_cotb(CFG, "x"))
        self.assertIn("inválido", hi._cmd_cotb(CFG, "a b c"))

    def test_cmd_cotb_edita_el_mensaje_de_las_cajitas(self):
        """F43c (Damian: 'en vez de mostrar el CoT abajo de todo,
        mostralo en el lugar donde está la cajita'): con ph en el cache,
        el contenido se EDITA en el mensaje de los botones con el teclado
        completo — no cae nada nuevo abajo."""
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["uno", "dos"],
                        "rotulos": ["CoT 1.1", "CoT 2.1"], "ph": 555}, f)
        edits = []

        def edit(cfg, ph, texto, markup=None):
            edits.append((ph, texto, markup))
            return True
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit", edit):
            out = hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertIsNone(out)  # nada nuevo abajo: fue un edit
        ph, texto, kb = edits[0]
        self.assertEqual(ph, 555)
        self.assertIn("<pre>dos</pre>", texto)
        self.assertEqual([b["text"] for fl in kb["inline_keyboard"]
                          for b in fl], ["CoT 1.1", "CoT 2.1"])

    def test_cmd_cotb_con_phs_edita_el_mensaje_de_su_razonada(self):
        """F46: con phs (una caja de botones por razonada), el tap edita el
        mensaje de SU razonada — no el ph único de siempre."""
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["uno", "dos"],
                        "rotulos": ["CoT 1.1", "CoT 2.1"],
                        "phs": [111, 222], "ph": 999}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, texto, markup=None:
                               (edits.append(ph), True)[1]):
            out = hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertIsNone(out)
        self.assertEqual(edits, [222])  # la 2ª razonada, no el 999

    def test_cmd_cotb_sin_ph_o_edit_fallido_cae_abajo_como_antes(self):
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                                        lambda *a, **k: False):
            self._cache(["uno"], ["CoT 1"])
            out = hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            self.assertIn("<pre>uno</pre>", out)


class TestOnStopSpawn(unittest.TestCase):
    """on_stop lanza --cot-botones solo si el vivo no tomó posesión."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.parchea(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        from zcode_bot.utils import http as zh
        support.parchea(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None: True)
        self.zt.set_active_bot_session(
            {"sid": SID, "title": "cv", "ws": "/w"})
        with open(self.zt.marker_path(SID), "w") as f:
            f.write("{}")

    def _cfg(self, **extra):
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1, notify_stop=True)
        cfg.update(extra)
        return cfg

    def test_spawnea_con_el_knob(self):
        self.zt.on_stop(self._cfg(mac_cot_botones=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})
        cot = [a for a in self.spawns if "--cot-botones" in a]
        self.assertEqual(len(cot), 1)
        i = cot[0].index("--cot-botones")
        self.assertEqual(cot[0][i + 1], SID)
        self.assertLess(abs(int(cot[0][i + 3]) - time.time()), 60)

    def test_knob_apagado_no_spawnea(self):
        self.zt.on_stop(self._cfg(mac_cot_botones=False),
                        {"sessionId": SID, "responseText": "listo"})
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_con_cache_del_vivo_no_spawnea(self):
        # el MISMO t0 que on_stop va a leer: el mtime del marker
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.guardar(SID, t0m, "cv", [], [], vivo=True)
        self.zt.on_stop(self._cfg(mac_cot_botones=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])


if __name__ == "__main__":
    unittest.main()
