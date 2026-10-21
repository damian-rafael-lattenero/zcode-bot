"""F41/F43/F46 — el CoT de la Mac EN VIVO: cajas por tamaño con botones.

El vigilante sondea el historial cada 3s; las cajas ya LLENAS viajan como
botones en cuanto se llenan (la caja de botones NACE con la primera), la
que se está llenando no viaja, y al cerrar el turno el texto final trae
el total. Modelo de Damian (F43): tope por caja. F46: rótulos «CoT R.C»
(razonada R, caja C — nunca dos «CoT 1») y UNA caja de botones por
razonada, cada una nacida en su posición cronológica. Base verificada en
vivo 20/10: el part reasoning del paso en curso ya es legible en
session/messages a mitad de paso.
"""
import json
import os
import sys
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot.utils import http as zh  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
# F51c: reciente — el aviso de turno largo dispara con t0 viejo y los
# tests del loop volarían con un ⏱️ extra adentro
# F51c: reciente (120s atrás: lejos del segundo del marker
# real del turno 2 en los pickups, lejos del umbral del aviso)
T0 = int(time.time()) - 120
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
CAP = rz.COT_CAJA_CAP


def _msg(rol, *parts):
    return {"info": {"role": rol}, "parts": list(parts)}


def _r(texto):
    return {"type": "reasoning", "text": texto}


class TestCajasVivas(unittest.TestCase):
    def test_la_caja_en_curso_no_viaja_hasta_que_se_llena(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("a" * (CAP + 700))),  # en curso: 1 llena + cola
        ]
        cajas, rotulos, pend, k = rz.cajas_vivas(msgs)
        self.assertEqual([len(c) for c in cajas], [CAP])
        self.assertEqual(rotulos, ["CoT 1.1"])
        self.assertEqual((pend, k), (700, "1.2"))

    def test_razonadas_cerradas_viajan_completas(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("a" * (CAP + 100)), {"type": "tool",
                                                       "tool": "Bash"}),
            _msg("assistant", _r("b" * 50), {"type": "text", "text": "fin"}),
        ]
        cajas, rotulos, pend, k = rz.cajas_vivas(msgs)
        self.assertEqual([len(c) for c in cajas], [CAP, 100, 50])
        self.assertEqual(rotulos, ["CoT 1.1", "CoT 1.2", "CoT 2.1"])  # R.C
        self.assertEqual(pend, 0)


class _ProtoCreciente:
    """El historial EVOLUCIONA entre polls (como el turno real):
    poll 1: una razonada en curso con 1 caja llena;
    poll 2: esa razonada cerró (cola sellada) y arrancó otra;
    poll 3 (fin): todo completo."""

    def __init__(self, marker):
        self.marker, self.vistos, self.cerrado = marker, 0, False
        A = "a" * (CAP + 500)
        B = "b" * 300
        self.fases = [
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r(B))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r(B), {"type": "text", "text": "fin"})],
        ]

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            return {"result": {}}
        if method == "session/messages":
            fase = min(self.vistos, len(self.fases) - 1)
            msgs = self.fases[fase]
            self.vistos += 1
            if self.vistos >= 3:  # F46: un poll ve la 2ª razonada cerrada
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
        self.sends, self.edits = [], []
        support.parchea(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), 777)[1])
        support.parchea(self, rz.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((text, markup)), True)[1])
        self.naps = []
        support.parchea(self, rz.time, "sleep", lambda s: self.naps.append(s))
        with open(rz.marker_path(SID), "w") as f:
            f.write("{}")

    def test_turno_largo_en_la_mac_avisa(self):
        # F51c: el turno de la Mac que se estira avisa ⏱️ con la MISMA
        # frase y el MISMO valor que el worker (R0b) — el silencio post-
        # cajas de un turno largo se leía como «se tildó» (21/10 07:43).
        class _ProtoCallado:
            """Sin eventos; a la vuelta k consume el marker (fin)."""

            def __init__(self, marker, vueltas=3):
                self.marker, self.vueltas, self.n = marker, vueltas, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > self.vueltas:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": []}}
                return {"result": {}}

            def close(self):
                pass

        proto = _ProtoCallado(rz.marker_path(SID))
        support.parchea(self, rz.zc_proto, "make",
                        lambda cwd=None, **kw: proto)
        t0 = int(time.time()) - 10
        support.parchea(self, rz.config, "TURNO_LARGO_AVISO_S", 0)
        rz.vivir(CFG, SID, "cv", t0, "/w")
        largos = [t for t, _mk in self.sends
                  if "lleva" in t and "min en la Mac" in t]
        self.assertEqual(len(largos), 1)  # una vez: gap ≥60s aunque el
        # umbral parcheado sea 0 (dedupe por próximo, no por bucle)
        self.assertIn("«cv»", largos[0])
        self.assertIn("Stop de la app", largos[0])

    def test_caja_nace_con_la_primera_llena_y_crece(self):
        proto = _ProtoCreciente(rz.marker_path(SID))
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.vivir(CFG, SID, "cv", T0, "/w")
        # 1) NACE con la primera caja LLENA (no hay mensaje «pensando» vacío):
        #    1 botón CoT 1.1, anunciando la caja que se llena
        n0, mk0 = self.sends[0]
        b0 = [b for f in mk0["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b0], ["CoT 1.1"])
        self.assertIn("llenando la CoT 1.2 (500 chars)", n0)
        # 2) la razonada 1 creció: edit con 2 botones SUYOS (1.1 y 1.2)
        n1, mk1 = self.edits[0]
        b1 = [b for f in mk1["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b1], ["CoT 1.1", "CoT 1.2"])
        # 3) F46: la 2ª razonada cerró → SU mensaje queda con estado final…
        nc, mkc = self.edits[1]
        bc = [b for f in mkc["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in bc], ["CoT 1.1", "CoT 1.2"])
        self.assertIn("razonada 1", nc)
        #    …y la 2ª nace en un mensaje NUEVO con SU único botón
        self.assertEqual(len(self.sends), 2)
        n2, mk2 = self.sends[1]
        b2 = [b for f in mk2["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b2], ["CoT 2.1"])
        # 4) final (F49): la ÚLTIMA razonada cierra como las demás — SU
        #    mensaje, SUS botones; ningún índice con todas acumulado al
        #    final. Cache finalizado con phs por caja.
        nf, mkf = self.edits[-1]
        bf = [b for f in mkf["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in bf], ["CoT 2.1"])
        self.assertIn("razonada 2", nf)
        self.assertIn("1 caja", nf)
        self.assertFalse(any("Razonamiento" in t for t, _m in self.edits))
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["cajas"]], [CAP, 500, 300])
        self.assertEqual(d["rotulos"], ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(d["phs"], [777, 777, 777])  # F46: ph por caja
        self.assertTrue(d["finalizado"])
        self.assertTrue(proto.cerrado)
        self.assertTrue(all(s == rz.COT_VIVO_CAD_S for s in self.naps))
        # el tap sobre la 3ª caja (2ª razonada) abre SU texto con SU rótulo
        out = rz.mostrar(SID, T0, 3)
        self.assertIn("<pre>%s</pre>" % ("b" * 300), out)
        self.assertIn("<b>CoT 2.1</b>", out)

    def test_resume_fallido_deja_el_paso_a_f40(self):
        proto = _ProtoCreciente(rz.marker_path(SID))

        def req(method, params=None, timeout=None):
            if method == "session/resume":
                return {"error": {"code": -1, "message": "boom"}}
            return {"result": {"events": []}}
        proto.request = req
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.vivir(CFG, SID, "cv", T0, "/w")
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertTrue(d.get("finalizado"))  # no queda colgado como dueño
        self.assertEqual(d["cajas"], [])


class TestSpawns(unittest.TestCase):
    """on_user_prompt lanza --cot-vivo; on_stop NO lanza --cot-botones si
    el vigilante ya es dueño (cache existe)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.parchea(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        support.parchea(self, zh, "send",
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
        vivo = next(a for a in self.spawns if "--cot-vivo" in a)
        t0_vivo = int(vivo[vivo.index("--cot-vivo") + 3])
        # F51a: la posesión del vivo = su pid-file (proceso vivo) + el
        # cache vivo=True que escribió — aunque esté resumiendo (heartbeat
        # vencido), on_stop no le roba el cierre.
        rz.pid_escribir(SID)
        rz.guardar(SID, t0_vivo, "cv", [], [], vivo=True)  # toma de posesión
        self._stop()
        self.assertEqual([a for a in self.spawns if "--cot-botones" in a], [])

    def test_on_stop_lanza_f40_si_no_hay_cache(self):
        with open(self.zt.marker_path(SID), "w") as f:
            f.write("{}")
        self._stop()
        self.assertEqual(len([a for a in self.spawns
                              if "--cot-botones" in a]), 1)


if __name__ == "__main__":
    unittest.main()
