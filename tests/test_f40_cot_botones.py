"""F40 — el razonamiento de los turnos de la Mac, con botones «CoT n».

Damian: "¿por qué el CoT de la Mac no se puede ver en Telegram? […] con
botones, que aparezcan cot1 cot2 y elegir cuál ver". ADR-017 cerró el
CoT EN VIVO; pero el historial (session/messages) persiste las parts
reasoning también en sesiones interactive — verificado 20/10. Al cerrar
el turno (on_stop) un subproceso las captura, cachea y manda botones.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

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
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"  # el formato real, largo


def _msg(rol, *parts):
    return {"info": {"role": rol}, "parts": list(parts)}


def _r(texto):
    return {"type": "reasoning", "text": texto}


HISTORIAL = [
    _msg("user", {"type": "text", "text": "primero"}),
    _msg("assistant", _r("razón vieja A"), {"type": "tool", "tool": "Read"}),
    _msg("assistant", _r("razón vieja B"), {"type": "text", "text": "ok"}),
    _msg("user", {"type": "text", "text": "segundo"}),
    _msg("assistant", _r("  razón 1 del turno  "), {"type": "step-finish"}),
    _msg("assistant", {"type": "tool", "tool": "Edit"}, _r("razón 2 del turno")),
    _msg("assistant", {"type": "reasoning", "text": "   "}),  # vacía: se tira
    _msg("assistant", {"type": "text", "text": "respuesta final"}),
]


class TestBloques(unittest.TestCase):
    def test_solo_el_ultimo_turno_en_orden(self):
        self.assertEqual(rz.bloques_del_turno(HISTORIAL),
                         ["razón 1 del turno", "razón 2 del turno"])

    def test_sin_user_toma_todo(self):
        self.assertEqual(rz.bloques_del_turno(HISTORIAL[4:6]),
                         ["razón 1 del turno", "razón 2 del turno"])

    def test_sin_reasoning(self):
        self.assertEqual(rz.bloques_del_turno([HISTORIAL[0], HISTORIAL[-1]]), [])


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

    def test_cachea_y_manda_un_boton_por_bloque(self):
        fake = _FakeProto(HISTORIAL)
        self._make(fake)
        n = rz.capturar(CFG, SID, "cv", T0, "/w")
        self.assertEqual(n, 2)
        self.assertEqual(fake.vistos, ["session/resume", "session/messages"])
        self.assertTrue(fake.cerrado)  # runtime de solo lectura: se cierra
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["bloques"], ["razón 1 del turno", "razón 2 del turno"])
        self.assertEqual(d["titulo"], "cv")
        texto, markup = self.sends[0]
        self.assertIn("🧠 <b>Razonamiento</b> de «cv»", texto)
        self.assertIn("2 bloques", texto)
        botones = [b for f in markup["inline_keyboard"] for b in f]
        self.assertEqual([b["text"] for b in botones], ["CoT 1", "CoT 2"])
        self.assertEqual(botones[1]["callback_data"], "ct:%s:%d:2" % (SID, T0))
        # el tope de Telegram para callback_data es 64 BYTES: con el sid
        # real largo (41 chars) + t0 + bloque + parte tiene que entrar
        for b in botones:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)
        parte = rz.botones_partes(SID, T0, 20, 12, 12)[-1][-1]["callback_data"]
        self.assertLessEqual(len(parte.encode()), 64)

    def test_sin_razonamiento_no_manda_nada(self):
        self._make(_FakeProto([HISTORIAL[0], HISTORIAL[-1]]))
        self.assertIsNone(rz.capturar(CFG, SID, "cv", T0, "/w"))
        self.assertEqual(self.sends, [])
        self.assertFalse(os.path.exists(rz.cache_path(SID, T0)))

    def test_resume_fallido_no_rompe(self):
        self._make(_FakeProto(HISTORIAL, resume_error="no such session"))
        self.assertIsNone(rz.capturar(CFG, SID, "cv", T0, "/w"))
        self.assertEqual(self.sends, [])

    def test_mas_de_20_bloques_capa_los_botones(self):
        msgs = [HISTORIAL[3]] + [_msg("assistant", _r("b%d" % i))
                                 for i in range(25)]
        self._make(_FakeProto(msgs))
        self.assertEqual(rz.capturar(CFG, SID, "cv", T0, "/w"), 25)
        texto, markup = self.sends[0]
        self.assertIn("botones para los primeros 20", texto)
        self.assertEqual(sum(len(f) for f in markup["inline_keyboard"]), 20)
        self.assertEqual([len(f) for f in markup["inline_keyboard"]], [5] * 4)


class TestMostrar(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _cache(self, bloques):
        os.makedirs(os.path.dirname(rz.cache_path(SID, T0)), exist_ok=True)
        with open(rz.cache_path(SID, T0), "w") as f:
            json.dump({"sid": SID, "titulo": "cv", "t0": T0,
                       "bloques": bloques}, f)

    def test_bloque_chico_entero(self):
        self._cache(["pienso <b>esto</b>", "y esto"])
        out = rz.mostrar(SID, T0, 1)
        self.assertIsInstance(out, str)
        self.assertIn("🧠 <b>CoT 1/2</b> · «cv»", out)
        self.assertIn("<pre>pienso &lt;b&gt;esto&lt;/b&gt;</pre>", out)

    def test_bloque_grande_en_partes_iguales_con_botones(self):
        grande = "".join("línea de razonamiento %04d\n" % i for i in range(400))
        self._cache([grande])
        texto, markup = rz.mostrar(SID, T0, 1)
        self.assertIn("parte 1/", texto)
        botones = [b for f in markup["inline_keyboard"] for b in f]
        total = len(botones)
        self.assertEqual(botones[0]["text"], "[1/%d]" % total)
        self.assertEqual(botones[1]["callback_data"],
                         "ct:%s:%d:1:2" % (SID, T0))
        # la parte 2 por su botón, y todas juntas reconstruyen el bloque
        t2, _ = rz.mostrar(SID, T0, 1, 2)
        self.assertIn("parte 2/%d" % total, t2)
        from zcode_bot.utils.formatting import partes_iguales
        partes = partes_iguales(grande, rz.COT_PARTE_CAP)
        self.assertEqual("".join(partes), grande)
        self.assertEqual(len(partes), total)
        self.assertTrue(all(len(p) <= rz.COT_PARTE_CAP for p in partes))

    def test_fuera_de_rango_y_sin_cache(self):
        self._cache(["uno"])
        self.assertIn("fuera de rango (1 a 1)", rz.mostrar(SID, T0, 5))
        self.assertIn("ya no está guardado", rz.mostrar(SID, T0 + 1, 1))

    def test_cmd_cotb_parsea_el_boton(self):
        self._cache(["uno", "dos"])
        texto = _cb_text("ct:%s:%d:2" % (SID, T0))
        self.assertEqual(texto, "/cotb %s %d 2" % (SID, T0))
        out = hi._cmd_cotb(CFG, texto.split(" ", 1)[1])
        self.assertIn("CoT 2/2", out)
        self.assertIn("<pre>dos</pre>", out)
        self.assertIn("Uso:", hi._cmd_cotb(CFG, "x"))
        self.assertIn("inválido", hi._cmd_cotb(CFG, "a b c"))


class TestOnStopSpawn(unittest.TestCase):
    """on_stop lanza --cot-botones (subproceso) solo con el knob prendido,
    task current, sin worker y con marker (t0)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.parchea(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        support.parchea(self, zc_hooks.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None: True)
        self.zt.set_active_bot_session(
            {"sid": SID, "title": "cv", "ws": "/w"})
        with open(self.zt.marker_path(SID), "w") as f:
            f.write("{}")

    def _cfg(self, **extra):
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1, notify_stop=True)
        cfg.update(extra)
        return cfg

    def _payload(self):
        return {"sessionId": SID, "responseText": "listo", "cwd": "/w",
                "title": "cv"}

    def test_spawnea_con_el_knob(self):
        self.zt.on_stop(self._cfg(mac_cot_botones=True), self._payload())
        cot = [a for a in self.spawns if "--cot-botones" in a]
        self.assertEqual(len(cot), 1)
        i = cot[0].index("--cot-botones")
        sid, titulo, t0, cwd = cot[0][i + 1:i + 5]
        self.assertEqual(sid, SID)
        self.assertEqual(cwd, "/w")
        self.assertLess(abs(int(t0) - time.time()), 60)  # mtime del marker

    def test_knob_apagado_no_spawnea(self):
        self.zt.on_stop(self._cfg(mac_cot_botones=False), self._payload())
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_sin_marker_no_hay_t0_ni_spawn(self):
        os.remove(self.zt.marker_path(SID))
        self.zt.on_stop(self._cfg(mac_cot_botones=True), self._payload())
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])


if __name__ == "__main__":
    unittest.main()
