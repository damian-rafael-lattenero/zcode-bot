"""F41/F43/F46 — the Mac's CoT LIVE: size-based boxes with buttons.

The watchdog polls the history every 3s; boxes already FULL travel as
buttons as soon as they fill (the button box is BORN with the first
one), the one still filling does not travel, and when the turn closes
the final text brings the total. Damian's model (F43): a cap per box.
F46: «CoT R.C» labels (reasoning block R, box C — never two «CoT 1»)
and ONE button box per reasoning block, each born at its chronological
position. Base verified live 20/10: the reasoning part of the step in
progress is already readable in session/messages mid-step.
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
# F51c: recent — the long-turn notice fires with an old t0 and the
# loop tests would blow up with an extra ⏱️ inside
# F51c: recent (120s ago: far from the real marker's second of
# turn 2 in the pickups, far from the notice threshold)
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
            _msg("assistant", _r("a" * (CAP + 700))),  # in progress: 1 full + remainder
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
    """The history EVOLVES between polls (like the real turn):
    poll 1: one reasoning block in progress with 1 full box;
    poll 2: that block closed (remainder sealed) and another started;
    poll 3 (end): everything complete."""

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
            if self.vistos >= 3:  # F46: one poll sees the 2nd reasoning block closed
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
        # F51c: a stretched Mac turn notifies ⏱️ with the SAME phrase and
        # the SAME value as the worker (R0b) — the post-box silence of a
        # long turn was read as the bot having frozen (21/10 07:43).
        class _ProtoCallado:
            """No events; on iteration k it consumes the marker (end)."""

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
                  if "has been running" in t and "min on the Mac" in t]
        self.assertEqual(len(largos), 1)  # once: gap ≥60s even though the
        # patched threshold is 0 (dedupe by next, not by loop)
        self.assertIn("«cv»", largos[0])
        self.assertIn("app's Stop", largos[0])

    def test_caja_nace_con_la_primera_llena_y_crece(self):
        proto = _ProtoCreciente(rz.marker_path(SID))
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.vivir(CFG, SID, "cv", T0, "/w")
        # 1) BORN with the first box FULL (no empty "thinking" message):
        #    1 button CoT 1.1, announcing the box being filled
        n0, mk0 = self.sends[0]
        b0 = [b for f in mk0["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b0], ["CoT 1.1"])
        self.assertIn("filling CoT 1.2 (500 chars)", n0)
        # 2) reasoning block 1 grew: edit with ITS 2 buttons (1.1 and 1.2)
        n1, mk1 = self.edits[0]
        b1 = [b for f in mk1["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b1], ["CoT 1.1", "CoT 1.2"])
        # 3) F46: the 2nd reasoning block closed → ITS message keeps the final state…
        nc, mkc = self.edits[1]
        bc = [b for f in mkc["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in bc], ["CoT 1.1", "CoT 1.2"])
        self.assertIn("pass 1", nc)
        #    …and the 2nd is born in a NEW message with ITS single button
        self.assertEqual(len(self.sends), 2)
        n2, mk2 = self.sends[1]
        b2 = [b for f in mk2["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in b2], ["CoT 2.1"])
        # 4) final (F49): the LAST reasoning block closes like the rest —
        #    ITS message, ITS buttons; no index with all of them piled up
        #    at the end. Cache finalized with phs per box.
        nf, mkf = self.edits[-1]
        bf = [b for f in mkf["inline_keyboard"] for b in f]
        self.assertEqual([x["text"] for x in bf], ["CoT 2.1"])
        self.assertIn("pass 2", nf)
        self.assertIn("1 box", nf)
        self.assertFalse(any("Reasoning" in t for t, _m in self.edits))
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["cajas"]], [CAP, 500, 300])
        self.assertEqual(d["rotulos"], ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(d["phs"], [777, 777, 777])  # F46: ph per box
        self.assertTrue(d["finalizado"])
        self.assertTrue(proto.cerrado)
        self.assertTrue(all(s == rz.COT_VIVO_CAD_S for s in self.naps))
        # the tap on the 3rd box (2nd reasoning block) opens ITS text with ITS label
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
        self.assertTrue(d.get("finalizado"))  # does not stay hung as the owner
        self.assertEqual(d["cajas"], [])


class TestSpawns(unittest.TestCase):
    """on_user_prompt spawns --cot-vivo; on_stop does NOT spawn
    --cot-botones when the watchdog already owns it (cache exists)."""

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
        # F51a: ownership by the live watcher = its pid-file (live
        # process) + the vivo=True cache it wrote — even if it is
        # summarizing (heartbeat expired), on_stop does not steal the
        # closing.
        rz.pid_escribir(SID)
        rz.guardar(SID, t0_vivo, "cv", [], [], vivo=True)  # takes ownership
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
