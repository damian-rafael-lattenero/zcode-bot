"""F42/F43 — the CoT of the PHONE's turns in size-based boxes.

Damian's model: "a box has a size; when the CoT reaches the cap, the box
fills up and is sent with its number; it continues and stops again at
the cap, and so buttons keep arriving until the CoT ends; then the chat
writes or sends a form; and when it thinks again, it sends the CoT
again numbered from 1". The worker feeds a SelladorCot with the
reasoning_delta: the box travels as a button AS SOON AS IT FILLS (live,
mid-reasoning), not when the phase closes.
"""
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402
from zcode_bot.live_view import LiveTurnView  # noqa: E402

CAP = rz.COT_CAJA_CAP


class _FakeC:
    def __init__(self, evs=None):
        self.evs = evs or []
        self.pedidos = []

    def request(self, method, params=None, timeout=None):
        self.pedidos.append((method, dict(params or {})))
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


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1)
    cfg.update(extra)
    return cfg


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def _view(self, c, cfg=None, **dep_extra):
        self.sends = []       # (text, markup)
        self.live_sends = []
        self.edits = []       # (mid, text, markup)
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append((text, markup)), 601)[1],
            "live_send": lambda cfg, text:
                (self.live_sends.append(text), 620)[1],
            "live_edit": lambda cfg, mid, text, markup=None:
                (self.edits.append((mid, text, markup)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: None,
        }
        deps.update(dep_extra)
        return LiveTurnView(c, cfg or _cfg(self.zt, cot_botones=True),
                            self.T1, 1760000000.0, "testing", 777, _Th(2),
                            last_id=None, deps=deps)

    def _botones(self, kb):
        return [b["text"] for fila in (kb or {}).get("inline_keyboard", [])
                for b in fila]


# One reasoning that crosses the cap mid-delta (button LIVE), closes by
# tool (sealed tail), and a SECOND reasoning with ITS own message.
_FASES = [
    _ev(1, "reasoning_delta", "x" * 2000),
    _ev(2, "reasoning_delta", "y" * 1500),   # total 3500 → BOX 1 FULL here
    _ev(3, "tool_input_start"), _ev(4, "tool_call"),  # closes: tail = box 2
    _ev(5, "reasoning_delta", "z" * 300),    # 2nd reasoning…
    _ev(6, "text_delta", "la respuesta"),    # …closes on writing: box 2.1
]


class TestCajasPorTamano(_Base):
    async def test_la_caja_viaja_apenas_se_llena_y_cada_razonada_su_mensaje(self):
        v = self._view(_FakeC(list(_FASES)))
        cot, texto, pasos, live_id = v.run()
        # the button box is BORN with the first one FULL (no empty message)
        primera = [s for s in self.sends if s[1]][0]
        self.assertIn("1 box", primera[0])
        self.assertIn("filling CoT 1.2", primera[0])
        self.assertEqual(self._botones(primera[1]), ["CoT 1.1"])
        # the sealed texts: box 1 = first 3000 chars, box 2 = 500 tail
        self.assertEqual(v.cot_cajas[0], "x" * 2000 + "y" * 1000)
        self.assertEqual(v.cot_cajas[1], "y" * 500)
        # F46: the 1st reasoning closes with its own final state (ITS buttons)…
        cierre1 = [e for e in self.edits if "pass 1" in e[1]][0]
        self.assertEqual(self._botones(cierre1[2]), ["CoT 1.1", "CoT 1.2"])
        # …and the 2nd is born in a NEW message with ITS single button
        segunda = [s for s in self.sends if s[1]][1]
        self.assertEqual(self._botones(segunda[1]), ["CoT 2.1"])
        self.assertEqual(v.cot_rotulos, ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        # F49: NO global index at the end — the last reasoning closes with
        # ITS message and ITS buttons (before it was edited with all of the
        # turn's: «en la última caja se acumularon todas»)
        cierre2 = [e for e in self.edits if "pass 2" in e[1]][-1]
        self.assertEqual(self._botones(cierre2[2]), ["CoT 2.1"])
        self.assertFalse(any("Reasoning" in e[1] for e in self.edits))
        self.assertTrue(all(v.cot_phs))  # every box reachable through its button
        # the tap opens box 3 (the 2nd reasoning) via the SAME path as the Mac
        out = rz.mostrar(self.T1, int(v.t0), 3)
        self.assertIn("<pre>%s</pre>" % ("z" * 300), out)
        self.assertIn("🧠 <b>CoT 2.1</b>", out)  # the label of ITS reasoning

    async def test_el_texto_de_cot_stream_no_se_manda(self):
        v = self._view(_FakeC(list(_FASES)),
                       cfg=_cfg(self.zt, cot_botones=True, cot_stream=True))
        v.run()
        self.assertFalse(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any("analysis done" in s[0] for s in self.sends))
        # cot_full stays WHOLE (nothing is released): the boxes cover all of it
        self.assertEqual("".join(v.cot_cajas),
                         "x" * 2000 + "y" * 1500 + "z" * 300)

    async def test_knob_apagado_comporta_como_antes(self):
        v = self._view(_FakeC(list(_FASES)),
                       cfg=_cfg(self.zt, cot_botones=False, cot_stream=True))
        v.run()
        # the CoT travels as text (permanent boxes) and there are no buttons
        self.assertTrue(any("CoT 1\n<pre>" in s[0] for s in self.sends))
        self.assertFalse(any(s[1] for s in self.sends))

    async def test_razonada_menos_del_tope_una_caja_al_cerrar(self):
        v = self._view(_FakeC([_ev(1, "reasoning_delta", "corto"),
                               _ev(2, "text_delta", "resp")]))
        v.run()
        self.assertEqual(v.cot_cajas, ["corto"])
        self.assertEqual(v.cot_rotulos, ["CoT 1.1"])
        final = [e for e in self.edits if "pass 1" in e[1]][-1]
        self.assertIn("1 box", final[1])

    async def test_la_caja_viva_muestra_estado_no_el_texto_crudo(self):
        """F43b — with buttons the CoT is NOT seen twice: the live box says
        how many boxes there are and which one is filling; the content
        lives behind the taps. Fake clock (10s per now()) so the edit's
        3s gate always passes, and events by PHASES like a real turn
        (think first, write afterwards)."""

        class _PorFases:
            def __init__(self, fases):
                self.fases, self.i = fases, 0
                self.pedidos = []

            def request(self, method, params=None, timeout=None):
                self.pedidos.append(method)
                f = min(self.i, len(self.fases) - 1)
                evs = [dict(e) for e in self.fases[f]]
                self.i += 1
                return {"result": {"events": evs}}

        largo = "r" * (CAP + 400)
        fases = [[_ev(1, "reasoning_delta", "pienso "),
                  _ev(2, "reasoning_delta", largo)],
                 [_ev(1, "reasoning_delta", "pienso "),
                  _ev(2, "reasoning_delta", largo),
                  _ev(3, "text_delta", "respuesta")]]
        # R12: shared fake clock (10s per now(), no sleep pacing)
        # so the edit's 3s gate always passes
        v = self._view(_PorFases(fases),
                       now=support.FakeClock(step_s=10.0,
                                             with_sleep=False).time)
        v.th = _Th(4)
        v.run()
        vivas = [tx for _m, tx, _mk in self.edits if "thinking" in tx]
        self.assertTrue(vivas)
        self.assertTrue(any("1 box · filling CoT 1.2" in x for x in vivas))
        for x in vivas:
            self.assertNotIn("<pre>", x)   # without the raw CoT text
            self.assertNotIn("rrrr", x)
        # with the knob off, the live box shows the text as always
        v2 = self._view(_PorFases([[_ev(1, "reasoning_delta", "pienso algo")],
                                   [_ev(1, "reasoning_delta", "pienso algo"),
                                    _ev(2, "text_delta", "respuesta")]]),
                        cfg=_cfg(self.zt, cot_botones=False),
                        now=support.FakeClock(step_s=10.0,
                                              with_sleep=False).time)
        v2.th = _Th(4)
        v2.run()
        vivas2 = [tx for _m, tx, _mk in self.edits if "thinking:" in tx]
        self.assertTrue(any("pienso algo" in x for x in vivas2))

    async def test_turno_sin_razonamiento_no_crea_caja(self):
        v = self._view(_FakeC([_ev(1, "text_delta", "directo")]))
        v.run()
        self.assertFalse(any(s[1] for s in self.sends))
        self.assertFalse(os.path.exists(rz.cache_path(self.T1, int(v.t0))))


class TestTelemetriaCotMsgs(_Base):
    """The «live fin» line of the pwlog: cot_msgs counted ONLY the text
    chunks from _cot_flush — which returns early in button mode — so in
    production it always read 0 even when the turn had published boxes
    (20/10 19:09: message 5499 with CoT 1.1/1.2 and `cot_msgs=0`). Now
    it counts the box messages BORN (edits don't add up) and the line
    carries cajas= to read the turn at a glance."""

    def _logs(self):
        logs = []
        return logs, (lambda *a: logs.append(" ".join(str(x) for x in a)))

    async def test_cuenta_los_mensajes_nacidos_no_los_edits(self):
        logs, pwlog = self._logs()
        v = self._view(_FakeC(list(_FASES)), pwlog=pwlog)
        v.run()
        # _FASES: reasoning 1 (born + grows by edit) and reasoning 2 (born);
        # the final index EDITS the 2nd one's message → 2 messages, 3 boxes
        self.assertEqual(v.cot_nmsg, 2)
        self.assertEqual(len([s for s in self.sends if s[1]]), 2)
        fin = [ln for ln in logs if ln.startswith("live fin:")][0]
        self.assertIn("cot_msgs=2", fin)
        self.assertIn("boxes=3", fin)

    async def test_cajas_sin_mensaje_van_en_un_solo_de_tardias(self):
        # the reasonings' sends FAIL (no message_id): on close a single
        # latecomer message is born with THOSE boxes (F49 — the old global
        # index no longer exists) and it counts as a message
        logs, pwlog = self._logs()
        support.patch(self, rz, "pwlog", pwlog)  # log_teclado writes here
        llamadas = []

        def send(cfg, text, loud=None, markup=None):
            llamadas.append((text, markup))
            return 601 if "as the turn closed" in text else None
        v = self._view(_FakeC(list(_FASES)), send=send, pwlog=pwlog)
        v.run()
        self.assertEqual(v.cot_nmsg, 1)
        self.assertEqual(v.cot_phs, [601, 601, 601])  # all 3 live there
        tardias = [m for t, m in llamadas if "as the turn closed" in t][0]
        self.assertEqual(self._botones(tardias),
                         ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        fin = [ln for ln in logs if ln.startswith("live fin:")][0]
        self.assertIn("cot_msgs=1 boxes=3", fin)
        # and the pwlog says which buttons that message ended up showing (F49)
        self.assertTrue(any("cot kb phone late ph=601 [3]" in ln
                            for ln in logs))

    async def test_modo_texto_sigue_contando_los_trozos(self):
        logs, pwlog = self._logs()
        v = self._view(_FakeC(list(_FASES)), pwlog=pwlog,
                       cfg=_cfg(self.zt, cot_botones=False, cot_stream=True))
        v.run()
        trozos = [s for s in self.sends if "· CoT " in s[0]]
        self.assertGreater(len(trozos), 0)
        self.assertEqual(v.cot_nmsg, len(trozos))
        fin = [ln for ln in logs if ln.startswith("live fin:")][0]
        self.assertIn("cot_msgs=%d" % len(trozos), fin)
        self.assertIn("boxes=0", fin)  # without buttons there are no sealed boxes


class TestNumeracionSinHuecosNiFusiones(_Base):
    """F48c — what the 20/10 e2e uncovered (a 32-min turn: «CoT 34.3»
    with 19 real reasonings; the next one: 17 boxes in 2 reasonings). Two
    defects: (1) after closing by tool, self.razon stayed full and every
    following tool re-closed an EMPTY reasoning, consuming an R number;
    (2) evento_write wasn't reset → from the first write on, a reasoning
    followed by TEXT didn't close and the next one merged into it."""

    async def test_cuatro_razonadas_reales_son_cuatro(self):
        evs = [
            _ev(1, "reasoning_delta", "a" * 100), _ev(2, "text_delta", "t1"),
            _ev(3, "reasoning_delta", "b" * 100), _ev(4, "text_delta", "t2"),
            _ev(5, "reasoning_delta", "c" * 100), _ev(6, "tool_input_start"),
            _ev(7, "tool_input_start"), _ev(8, "tool_input_start"),
            _ev(9, "reasoning_delta", "d" * 100), _ev(10, "text_delta", "t3"),
        ]
        v = self._view(_FakeC(evs))
        v.run()
        # before: ['CoT 1.1', 'CoT 2.1', 'CoT 5.1'] with box 2 = b+c
        self.assertEqual(v.cot_rotulos,
                         ["CoT 1.1", "CoT 2.1", "CoT 3.1", "CoT 4.1"])
        self.assertEqual(v.cot_cajas, ["a" * 100, "b" * 100, "c" * 100,
                                       "d" * 100])
        # one button box PER reasoning, at its moment
        self.assertEqual(len([s for s in self.sends if s[1]]), 4)
        self.assertEqual(v.cot_nmsg, 4)

    async def test_tools_seguidas_tras_pensar_no_consumen_numeros(self):
        evs = [
            _ev(1, "reasoning_delta", "a" * 50),
            _ev(2, "tool_input_start"), _ev(3, "tool_call"),
            _ev(4, "tool_input_start"), _ev(5, "tool_call"),
            _ev(6, "tool_input_start"), _ev(7, "tool_call"),
            _ev(8, "reasoning_delta", "b" * 50), _ev(9, "tool_input_start"),
        ]
        v = self._view(_FakeC(evs))
        v.run()
        self.assertEqual(v.cot_rotulos, ["CoT 1.1", "CoT 2.1"])
        self.assertEqual(v.cot_razonada, 3)  # the next one would be 3, not 6

    async def test_aviso_de_fase_del_modo_texto_sigue_siendo_uno(self):
        # the evento_write guard remains ONLY for the text-mode notice
        evs = [
            _ev(1, "reasoning_delta", "a" * 50), _ev(2, "text_delta", "t1"),
            _ev(3, "reasoning_delta", "b" * 50), _ev(4, "text_delta", "t2"),
        ]
        v = self._view(_FakeC(evs),
                       cfg=_cfg(self.zt, cot_botones=False, cot_stream=True))
        v.run()
        avisos = [s for s in self.sends if "analysis done" in s[0]]
        self.assertEqual(len(avisos), 1)


if __name__ == "__main__":
    unittest.main()
