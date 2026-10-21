"""F40/F43/F46 — capture at close with SIZE-based BOXES (fallback of the live watcher).

Damian's model (F43): boxes sealed upon reaching the cap
(COT_CAJA_CAP), one button per box. F46: labels «CoT R.C» — reasoning R,
box C within it (never two «CoT 1»). capturar() is the fallback
for when the live watcher failed to start.
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
    _msg("assistant", {"type": "reasoning", "text": "   "}),  # empty: dropped
    _msg("assistant", {"type": "text", "text": "respuesta final"}),
]


class TestCajasDelTurno(unittest.TestCase):
    def test_dos_razonadas_rotulos_razonada_caja(self):
        """The tool closes the 1st reasoning; each fills ONE box — F46:
        «CoT 1.1» and «CoT 2.1», distinct (never two «CoT 1»)."""
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
    """The heart of F43: seals at the cap, close() seals the tail, restarts."""

    def test_sella_a_medida_que_se_llena(self):
        s = rz.SelladorCot(cap=100)
        self.assertEqual(s.alimentar("x" * 60), [])          # 60: nothing
        nuevas = s.alimentar("y" * 60)                        # 120 → box 1
        self.assertEqual([(c, n) for c, n in nuevas],
                         [("x" * 60 + "y" * 40, 1)])
        # 320 total: the same delta fills boxes 2 and 3 TOGETHER
        nuevas = s.alimentar("z" * 200)
        self.assertEqual([n for _c, n in nuevas], [2, 3])
        self.assertEqual(s.alimentar("w" * 10), [])  # 330 < 400: nothing

    def test_cerrar_sella_la_cola_y_reinicia(self):
        s = rz.SelladorCot(cap=100)
        s.alimentar("a" * 150)  # box 1 sealed, tail of 50
        self.assertEqual(s.cerrar(), [("a" * 50, 2)])          # tail = box 2
        self.assertEqual(s.cerrar(), [])                       # already empty
        nuevas = s.alimentar("b" * 120)                        # RESTARTED: 1
        self.assertEqual([n for _c, n in nuevas], [1])

    def test_en_curso(self):
        s = rz.SelladorCot(cap=100)
        s.alimentar("a" * 250)  # 2 sealed, 50 in progress → box 3
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
        # two reasonings → «CoT 1.1» and «CoT 2.1»: distinct, global index
        self.assertEqual([b["text"] for b in botones], ["CoT 1.1", "CoT 2.1"])
        self.assertEqual(botones[1]["callback_data"], "ct:%s:%d:2" % (SID, T0))
        for b in botones:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_sin_razonamiento_avisa_en_una_linea(self):
        """F45: the turn closed without reasoning → 0 boxes and ONE «sin CoT» line
        (no keyboard, no cache) — before, silence was indistinguishable
        from a bug. With the knob off, the usual silence."""
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
        gigante = "l" * (rz.COT_CAJA_CAP * 22)  # one reasoning → 22 boxes
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
        # compat: old F40 cache (bloques, no rotulos) + giant box
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
        self.assertIn("🧠 <b>CoT 2.1</b>", out)  # the label of ITS own reasoning
        self.assertIn("<pre>dos</pre>", out)
        self.assertIn("Uso:", hi._cmd_cotb(CFG, "x"))
        self.assertIn("inválido", hi._cmd_cotb(CFG, "a b c"))

    def test_cmd_cotb_edita_el_mensaje_de_las_cajitas(self):
        """F43c (Damian: 'instead of showing the CoT all the way at the
        bottom, show it where the little box is'): with ph in the cache,
        the content is EDITED into the button message with the full
        keyboard — nothing new drops below."""
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
        self.assertIsNone(out)  # nothing new below: it was an edit
        ph, texto, kb = edits[0]
        self.assertEqual(ph, 555)
        self.assertIn("<pre>dos</pre>", texto)
        self.assertEqual([b["text"] for fl in kb["inline_keyboard"]
                          for b in fl], ["CoT 1.1", "CoT 2.1"])

    def test_cmd_cotb_con_phs_edita_el_mensaje_de_su_razonada(self):
        """F46: with phs (one button-box per reasoning), the tap edits the
        message of ITS own reasoning — not the usual single ph."""
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
        self.assertEqual(edits, [222])  # the 2nd reasoning, not 999

    def test_cmd_cotb_teclado_de_las_cajas_de_ese_mensaje(self):
        """F49 (Damian: «I tapped thinking 1.1 and buttons from 2.1
        suddenly appeared» / «they all piled up in the last box»): the
        viewer edits with the buttons of the boxes that LIVE in that
        message — its own reasoning, the last one too (there is no longer
        a «hub» with the full index; that remains only for the old
        single-message cache, test_cmd_cotb_edita_el_mensaje_de_las_cajitas)."""
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["uno", "dos", "tres"],
                        "rotulos": ["CoT 1.1", "CoT 1.2", "CoT 2.1"],
                        "phs": [111, 111, 222], "ph": 222}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, texto, markup=None:
                               (edits.append((ph, markup)), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            hi._cmd_cotb(CFG, "%s %d 3" % (SID, T0))

        def textos(kb):
            return [b["text"] for fl in kb["inline_keyboard"] for b in fl]
        self.assertEqual(edits[0][0], 111)
        self.assertEqual(textos(edits[0][1]), ["CoT 1.1", "CoT 1.2"])
        self.assertEqual(edits[1][0], 222)
        self.assertEqual(textos(edits[1][1]), ["CoT 2.1"])  # the last one, same
        # the button callback is still the GLOBAL index
        cb = [b["callback_data"] for fl in edits[0][1]["inline_keyboard"]
              for b in fl]
        self.assertEqual(cb, ["ct:%s:%d:1" % (SID, T0),
                              "ct:%s:%d:2" % (SID, T0)])

    def test_cmd_cotb_tardias_comparten_mensaje(self):
        # F49: two reasonings without their own message live in the same
        # late-comers message → the viewer shows BOTH
        import json as _json
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": ["uno", "dos", "tres"],
                        "rotulos": ["CoT 1.1", "CoT 2.1", "CoT 3.1"],
                        "phs": [111, 333, 333], "ph": 111}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, texto, markup=None:
                               (edits.append((ph, markup)), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 2" % (SID, T0))
        self.assertEqual(edits[0][0], 333)
        self.assertEqual([b["text"] for fl in edits[0][1]["inline_keyboard"]
                          for b in fl], ["CoT 2.1", "CoT 3.1"])

    def test_cmd_cotb_partes_arriba_y_cajas_abajo(self):
        # F48b: a big box is navigated in parts WITHOUT losing the boxes
        # of its reasoning (before, parts replaced the whole keyboard)
        import json as _json
        grande = "".join("línea %04d\n" % i for i in range(400))
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            _json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                        "cajas": [grande, "dos", "tres"],
                        "rotulos": ["CoT 1.1", "CoT 1.2", "CoT 2.1"],
                        "phs": [111, 111, 222], "ph": 222}, f)
        edits = []
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                               lambda cfg, ph, texto, markup=None:
                               (edits.append(markup), True)[1]):
            hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
        filas = edits[0]["inline_keyboard"]
        self.assertTrue(filas[0][0]["text"].startswith("[1/"))  # parts
        self.assertEqual([b["text"] for b in filas[-1]],
                         ["CoT 1.1", "CoT 1.2"])              # its boxes

    def test_cmd_cotb_sin_ph_o_edit_fallido_cae_abajo_como_antes(self):
        from zcode_bot.utils import http as zh
        with mock.patch.object(zh, "_live_edit",
                                        lambda *a, **k: False):
            self._cache(["uno"], ["CoT 1"])
            out = hi._cmd_cotb(CFG, "%s %d 1" % (SID, T0))
            self.assertIn("<pre>uno</pre>", out)


class TestOnStopSpawn(unittest.TestCase):
    """on_stop spawns --cot-botones only if the live watcher didn't take over."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        self.sends = []
        support.parchea(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        from zcode_bot.utils import http as zh
        support.parchea(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None:
                            self.sends.append(text) or True)
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

    def test_con_cache_finalizado_no_spawnea(self):
        # the SAME t0 on_stop will read: the live watcher closed its cache
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.guardar(SID, t0m, "cv", [], [], finalizado=True)
        self.zt.on_stop(self._cfg(mac_cot_botones=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_cache_vivo_con_vigilante_no_spawnea(self):
        # F51a: the live watcher is IN FLIGHT (live pid, maybe resuming
        # with the heartbeat expired) — the cache hasn't closed but its owner exists
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.pid_escribir(SID)
        rz.guardar(SID, t0m, "cv", [], [], vivo=True)
        self.zt.on_stop(self._cfg(mac_cot_botones=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_cache_vivo_sin_vigilante_spawnea(self):
        # F51a (the real case): the watcher DIED midway and its cache stayed
        # vivo=True — before, that cache silenced the fallback and the
        # turn closed with NO boxes. Now the fallback runs anyway.
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.guardar(SID, t0m, "cv", ["x"], ["CoT 1.1"], vivo=True)
        self.zt.on_stop(self._cfg(mac_cot_botones=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})
        cot = [a for a in self.spawns if "--cot-botones" in a]
        self.assertEqual(len(cot), 1)

    def test_el_check_anuncia_el_cierre_lento(self):
        # F51a: without the live watcher's cache the fallback takes ~2 min —
        # the ✅ says so, so the delayed boxes aren't read as another turn's
        self.zt.on_stop(self._cfg(mac_cot_botones=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})
        self.assertTrue(any("cajas del razonamiento en camino" in t
                            and "Turno terminado" in t for t in self.sends))

    def test_con_cache_el_check_no_anuncia_demora(self):
        t0m = int(os.path.getmtime(self.zt.marker_path(SID)))
        rz.guardar(SID, t0m, "cv", [], [], finalizado=True)
        self.zt.on_stop(self._cfg(mac_cot_botones=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})
        self.assertFalse(any("en camino" in t for t in self.sends))


if __name__ == "__main__":
    unittest.main()
