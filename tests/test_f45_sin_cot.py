"""F45 — a turn that closes WITHOUT reasoning says so (before: silence).

Diagnosis from 20/10 on the raw runtime rollout: the bot requests
IDENTICAL thinking for GLM-5.3 and Flash (thinking enabled + effort
max); GLM-5.3 reasons ADAPTIVELY — it decides per call — and in the
bot's session it went 12 calls in a row without reasoning with ~130k
tokens of context (it came back after compacting); Flash reasons
almost always. Zero boxes and no message whatsoever was
indistinguishable from a bug: now all three sources send ONE
«sin CoT» line (knob cot_aviso_vacio). capturar() is covered in
test_f40.
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
from zcode_bot.live_view import LiveTurnView  # noqa: E402

SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
# F51c: recent — the long-turn notice fires with an old t0
# F51c: recent (120s ago: far from the seconds of the real
# marker of turn 2 in the pickups, far from the notice threshold)
T0 = int(time.time()) - 120


# ------------------------------------------------------------- phone (F42)

class _FakeC:
    def __init__(self, evs):
        self.evs = evs
        self.pedidos = []

    def request(self, method, params=None, timeout=None):
        self.pedidos.append(method)
        return {"result": {"events": [dict(e) for e in self.evs]}}


class _Th:
    def __init__(self, n=2):
        self.n = n

    def is_alive(self):
        self.n -= 1
        return self.n > 0


def _ev(eid, kind, delta=""):
    p = {"kind": kind}
    if delta:
        p["delta"] = delta
    return {"eventId": eid, "payload": p}


class TestCelu(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _view(self, evs, cfg_extra=None, deps_extra=None):
        self.sends = []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append((text, markup)), 601)[1],
            "live_send": lambda cfg, text: 620,
            "live_edit": lambda cfg, mid, text, markup=None: True,
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: None,
        }
        deps.update(deps_extra or {})
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1, cot_botones=True)
        cfg.update(cfg_extra or {})
        return LiveTurnView(_FakeC(evs), cfg, support.T1, float(T0), "testing",
                            777, _Th(2), last_id=None, deps=deps)

    def _avisos(self):
        return [s for s in self.sends if "sin CoT" in s[0]]

    async def test_turno_sin_razonamiento_avisa_en_una_linea(self):
        v = self._view([_ev(1, "text_delta", "respuesta directa")])
        v.run()
        self.assertEqual(len(self._avisos()), 1)
        texto, markup = self._avisos()[0]
        self.assertIn("«testing»", texto)
        self.assertIsNone(markup)  # one line: no keyboard
        # no boxes means no cache: nothing to touch, nothing to purge
        self.assertFalse(os.path.exists(rz.cache_path(support.T1, T0)))

    async def test_knob_apagado_calla(self):
        v = self._view([_ev(1, "text_delta", "directo")],
                       cfg_extra={"cot_aviso_vacio": False})
        v.run()
        self.assertEqual(self._avisos(), [])

    async def test_sin_deltas_es_un_turno_de_la_mac_y_calla(self):
        """Without a single delta the turn was run by the Mac app (its
        stream never crosses to the bot): whether it reasoned is
        unknown — stay silent."""
        v = self._view([])
        v.run()
        self.assertEqual(self._avisos(), [])

    async def test_stop_del_usuario_calla(self):
        v = self._view([_ev(1, "text_delta", "a medias")],
                       deps_extra={"stop_requested": lambda sid: True})
        v.run()
        self.assertEqual(v.stop_pedidos, 1)
        self.assertEqual(self._avisos(), [])

    async def test_con_razonamiento_no_avisa(self):
        v = self._view([_ev(1, "reasoning_delta", "pienso"),
                        _ev(2, "text_delta", "resp")])
        v.run()
        self.assertEqual(v.cot_cajas, ["pienso"])
        self.assertEqual(self._avisos(), [])


# ------------------------------------------------------------- Mac (F41/F44)

def _msg(rol, *parts):
    return {"info": {"role": rol}, "parts": list(parts)}


SIN_RAZON = [_msg("user", {"type": "text", "text": "ok?"}),
             _msg("assistant", {"type": "text", "text": "ok."})]


class _ProtoGuion:
    """session/messages returns the next step: (msgs, action) with the
    hook's action on the marker — "rm" (on_stop: it closed) or ("new",
    tok) (a new prompt that cuts the turn). Once the script is exhausted
    it repeats the last history (the closing fetch arrives AFTER the
    rm)."""

    def __init__(self, marker, pasos):
        self.marker, self.pasos, self.cerrado = marker, list(pasos), False
        self.ultimos = []

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            return {"result": {}}
        if method == "session/messages":
            if self.pasos:
                msgs, accion = self.pasos.pop(0)
                self.ultimos = msgs
            else:
                msgs, accion = self.ultimos, "rm"
            if accion == "rm":
                try:
                    os.remove(self.marker)
                except OSError:
                    pass
            elif accion:
                with open(self.marker, "w") as f:
                    json.dump({"token": accion[1]}, f)
            return {"result": {"messages": msgs}}
        return {"result": {}}

    def close(self):
        self.cerrado = True


class TestMacVivo(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits = [], []
        support.parchea(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), 777)[1])
        support.parchea(self, rz.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((ph, text, markup)), True)[1])
        support.parchea(self, rz.time, "sleep", lambda s: None)
        with open(rz.marker_path(SID), "w") as f:
            json.dump({"token": "tok1"}, f)  # with a token: another token = cut
        self.cfg = {"token": "T", "chat_id": 1, "cot_aviso_vacio": True}

    def _vivir(self, pasos, cfg=None):
        proto = _ProtoGuion(rz.marker_path(SID), pasos)
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.vivir(cfg or self.cfg, SID, "cv", T0, "/w")
        return proto

    def _sin_cot(self):
        return [t for _p, t, _m in self.edits if "sin CoT" in t]

    def test_la_caja_viva_termina_diciendo_sin_cot(self):
        proto = self._vivir([(SIN_RAZON, None), (SIN_RAZON, "rm")])
        # the box was born on the first poll (0 boxes)…
        self.assertEqual(len(self.sends), 1)
        self.assertIn("0 cajas", self.sends[0][0])
        # …and at close it says «sin CoT» (before: «0 cajas, 0 chars. Tocá
        # una para leerla» without a single button)
        ph, texto, markup = self.edits[-1]
        self.assertEqual(ph, 777)
        self.assertIn("sin CoT", texto)
        self.assertIn("«cv»", texto)
        self.assertIsNone(markup)
        with open(rz.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["cajas"], [])
        self.assertTrue(d["finalizado"])
        self.assertTrue(proto.cerrado)

    def test_knob_apagado_deja_el_cierre_de_siempre(self):
        self._vivir([(SIN_RAZON, "rm")],
                    cfg={"token": "T", "chat_id": 1, "cot_aviso_vacio": False})
        self.assertEqual(self._sin_cot(), [])
        self.assertIn("Razonamiento", self.edits[-1][1])

    def test_sin_caja_viva_manda_una_linea(self):
        """The live box was never born (send returned no id): the notice
        goes out as its own line, with no keyboard."""
        support.parchea(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), False)[1])
        self._vivir([(SIN_RAZON, "rm")])
        avisos = [s for s in self.sends if "sin CoT" in s[0]]
        self.assertEqual(len(avisos), 1)
        self.assertIsNone(avisos[0][1])
        self.assertEqual(self.edits, [])

    def test_corte_ajeno_no_afirma_nada(self):
        """The marker was replaced by a new prompt mid-way through turn 1:
        what was seen live is all there is and whether it reasoned is
        NOT known → turn 1 closes as always; turn 2 (chained, closed
        cleanly without reasoning) does give the notice."""
        self._vivir([(SIN_RAZON, ("new", "tok2")), (SIN_RAZON, "rm")])
        self.assertEqual(len(self._sin_cot()), 1)
        self.assertIn("Razonamiento", self.edits[0][1])  # turn 1: legacy close
        self.assertIn("sin CoT", self.edits[-1][1])      # turn 2: the notice

    def test_historial_vacio_calla(self):
        """It closed but the history brought nothing back (failed fetch /
        lagging store): unknown → neither a notice nor an invented
        close."""
        self._vivir([([], "rm")])
        self.assertEqual(self._sin_cot(), [])
        self.assertFalse(any("sin CoT" in s[0] for s in self.sends))


class TestTexto(unittest.TestCase):
    def test_texto_vacio_lleva_titulo_hora_y_escapa(self):
        out = rz.texto_vacio("a<b>", T0)
        self.assertIn("sin CoT", out)
        self.assertIn("a&lt;b&gt;", out)
        self.assertIn("turno de las", out)
        self.assertNotIn("<pre>", out)


if __name__ == "__main__":
    unittest.main()
