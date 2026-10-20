"""F41 — el CoT de la Mac EN VIVO: botones que crecen mientras piensa.

Diseño textual de Damian: "recibí prompt → estoy pensando y generando CoT
→ generé un CoT que superó límite, creo cajitas 1 y 2 y 3 → sigo
generando → cajitas 4 y 5 mientras genero muestro botones". Cada bloque
NO se manda: se manda su botón-referencia, editando el teclado del mismo
mensaje. Base verificada en vivo 20/10: el part reasoning del paso en
curso ya está en session/messages mientras el turno corre (6.7k chars a
mitad de paso).
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
from zcode_bot.utils import http as zc_http  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
T0 = 1760000000
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"


def _msg(rol, *parts):
    return {"info": {"role": rol}, "parts": list(parts)}


def _r(texto):
    return {"type": "reasoning", "text": texto}


class TestBloquesVivos(unittest.TestCase):
    def test_el_ultimo_part_del_ultimo_mensaje_esta_en_curso(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("bloque 1"), {"type": "tool", "tool": "Bash"}),
            _msg("assistant", {"type": "step-start"}, _r("creciendo…")),
        ]
        bloques, pend = rz.bloques_vivos(msgs)
        self.assertEqual(bloques, ["bloque 1"])
        self.assertEqual(pend, len("creciendo…"))

    def test_cuando_cierra_el_paso_el_bloque_pasa_a_completo(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("bloque 1"), {"type": "tool", "tool": "Bash"}),
            _msg("assistant", {"type": "step-start"}, _r("bloque 2"),
                 {"type": "text", "text": "respuesta"}),
        ]
        bloques, pend = rz.bloques_vivos(msgs)
        self.assertEqual(bloques, ["bloque 1", "bloque 2"])
        self.assertEqual(pend, 0)

    def test_varios_bloques_en_un_mismo_mensaje(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("a"), _r("b"), _r("creciendo")),
        ]
        bloques, pend = rz.bloques_vivos(msgs)
        self.assertEqual(bloques, ["a", "b"])
        self.assertEqual(pend, len("creciendo"))


class _ProtoCreciente:
    """session/messages que EVOLUCIONA entre polls (como el turno real):
    poll 1: un bloque completo + uno creciendo; poll 2: dos completos + el
    segundo creció; poll 3 (fin): todo completo."""

    def __init__(self, marker):
        self.marker, self.vistos, self.cerrado = marker, 0, False
        self.fases = [
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r("uno"), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r("par"))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r("uno"), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r("parcial que crece"), {"type": "step-finish"}),
             _msg("assistant", _r("tre"))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r("uno"), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r("dos completo"), {"type": "step-finish"}),
             _msg("assistant", _r("tres"), {"type": "text", "text": "fin"})],
        ]

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            return {"result": {}}
        if method == "session/messages":
            fase = min(self.vistos, len(self.fases) - 1)
            msgs = self.fases[fase]
            self.vistos += 1
            if self.vistos >= 2:  # en el 3er poll el turno ya cerró
                try:
                    os.remove(self.marker)
                except OSError:
                    pass
            return {"result": {"messages": msgs}}
        return {"result": {}}

    def close(self):
        self.cerrado = True


class TestVivir(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits, self.cache_en_edit = [], [], []
        support.parchea(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), 777)[1])

        def edit(cfg, ph, text, markup=None):
            self.edits.append((text, markup))
            if not self.cache_en_edit:  # snapshot del cache al 1er edit
                try:
                    with open(rz.cache_path(SID, T0)) as f:
                        self.cache_en_edit.append(json.load(f))
                except OSError:
                    pass
            return True
        support.parchea(self, rz.uhttp, "_live_edit", edit)
        self.naps = []

        def dormi(s):
            self.naps.append(s)
        support.parchea(self, rz.time, "sleep", dormi)
        with open(rz.marker_path(SID), "w") as f:
            f.write("{}")

    def test_caja_viva_teclado_que_crece_y_final(self):
        proto = _ProtoCreciente(rz.marker_path(SID))
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.vivir(CFG, SID, "cv", T0, "/w")
        # 1) caja inicial apenas spawnea (antes del resume)
        self.assertIn("pensando", self.sends[0])
        # 2) el cache es dueño desde el ARRANQUE (F40 no duplica): al primer
        # edit ya existe, con los bloques completos HASTA ese momento
        self.assertTrue(self.cache_en_edit)
        self.assertTrue(self.cache_en_edit[0].get("vivo"))
        self.assertEqual(self.cache_en_edit[0]["bloques"], ["uno"])
        # 3) edits con teclado creciente: 1 → 2 → final 3; el intermedio
        # anuncia el bloque en curso
        marcados = [len([b for fl in (mk or {}).get("inline_keyboard", [])
                         for b in fl]) for tx, mk in self.edits]
        self.assertEqual(marcados, [1, 2, 3])
        self.assertIn("escribiendo uno", self.edits[1][0])
        final_tx, final_mk = self.edits[-1]
        self.assertIn("Razonamiento", final_tx)
        self.assertIn("3 bloques", final_tx)
        botones = [b["text"] for fl in final_mk["inline_keyboard"] for b in fl]
        self.assertEqual(botones, ["CoT 1", "CoT 2", "CoT 3"])
        # 4) cache final: completo y finalizado — los taps siguen andando
        with open(rz.cache_path(SID, T0)) as f:
            d1 = json.load(f)
        self.assertEqual(d1["bloques"], ["uno", "dos completo", "tres"])
        self.assertTrue(d1.get("finalizado"))
        out = rz.mostrar(SID, T0, 2)
        self.assertIn("<pre>dos completo</pre>", out)
        # 5) el runtime de solo lectura se cierra y el loop durmió su cadencia
        self.assertTrue(proto.cerrado)
        self.assertTrue(all(s == rz.COT_VIVO_CAD_S for s in self.naps))

    def test_resume_fallido_deja_el_paso_a_f40(self):
        proto = _ProtoCreciente(rz.marker_path(SID))

        def req(method, params=None, timeout=None):
            if method == "session/resume":
                return {"error": {"code": -1, "message": "boom"}}
            return proto.request(method, params, timeout)
        proto.request = req
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.vivir(CFG, SID, "cv", T0, "/w")
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertTrue(d.get("finalizado"))  # no queda colgado como dueño
        self.assertEqual([b for b in d["bloques"]], [])


class TestSpawns(unittest.TestCase):
    """on_user_prompt lanza --cot-vivo; on_stop NO lanza --cot-botones si
    el vigilante ya es dueño (cache existe)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.parchea(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        support.parchea(self, zc_hooks.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None: True)
        self.zt.set_active_bot_session(
            {"sid": SID, "title": "cv", "ws": "/w"})
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        notify_prompt=True, mac_cot_botones=True)

    def test_prompt_spawnea_cot_vivo(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": SID, "cwd": "/w",
                                          "title": "cv", "prompt": "p"})
        vivo = [a for a in self.spawns if "--cot-vivo" in a]
        self.assertEqual(len(vivo), 1)
        i = vivo[0].index("--cot-vivo")
        self.assertEqual(vivo[0][i + 1], SID)
        self.assertEqual(vivo[0][i + 3], str(int(os.path.getmtime(
            self.zt.marker_path(SID)))))

    def test_knob_apagado_no_spawnea(self):
        self.zt.on_user_prompt(dict(self.cfg, mac_cot_botones=False),
                               {"sessionId": SID, "prompt": "p"})
        self.assertEqual([a for a in self.spawns if "--cot-vivo" in a], [])

    def _stop(self):
        self.zt.on_stop(dict(self.cfg, notify_stop=True),
                        {"sessionId": SID, "responseText": "listo",
                         "cwd": "/w", "title": "cv"})

    def test_on_stop_salta_f40_si_el_vivo_es_dueno(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": SID, "cwd": "/w",
                                          "title": "cv", "prompt": "p"})
        # el vigilante real (Popen fakeado) escribe el cache al arrancar:
        # simular esa toma de posesión con el MISMO t0 que usó el spawn
        vivo = next(a for a in self.spawns if "--cot-vivo" in a)
        t0_vivo = int(vivo[vivo.index("--cot-vivo") + 3])
        os.makedirs(os.path.dirname(rz.cache_path(SID, t0_vivo)),
                    exist_ok=True)
        with open(rz.cache_path(SID, t0_vivo), "w") as f:
            json.dump({"sid": SID, "t0": t0_vivo, "bloques": [], "vivo": True}, f)
        self._stop()
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_on_stop_lanza_f40_si_no_hay_cache(self):
        # marker vivo (lo escribe on_user_prompt) pero SIN cache: nadie
        # tomó posesión → F40 captura al cierre
        with open(self.zt.marker_path(SID), "w") as f:
            f.write("{}")
        self._stop()
        self.assertEqual(len([a for a in self.spawns
                              if "--cot-botones" in a]), 1)


class TestLiveEditMarkup(unittest.TestCase):
    def test_markup_viaja_como_reply_markup(self):
        params_capturados = []

        def api(cfg, metodo, params):
            params_capturados.append((metodo, params))
            return {"result": {"message_id": 1}}
        kb = {"inline_keyboard": [[{"text": "CoT 1", "callback_data": "ct:x"}]]}
        with mock.patch.object(zc_http, "tg_api", api), \
                mock.patch.object(zc_http, "_cooldown_until", [0]):
            self.assertTrue(zc_http._live_edit(CFG, 9, "texto", markup=kb))
            self.assertTrue(zc_http._live_edit(CFG, 9, "texto"))
        con_kb, sin_kb = params_capturados
        self.assertEqual(con_kb[0], "editMessageText")
        self.assertIs(con_kb[1]["reply_markup"], kb)
        self.assertNotIn("reply_markup", sin_kb[1])


if __name__ == "__main__":
    unittest.main()
