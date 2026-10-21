# -*- coding: utf-8 -*-
"""LiveTurnView test battery (F4d) — the live view of the turn.

It used to be 242 lines with 2 tests (only the F15 pause). Here the rest
of the contract is frozen: streaming of deltas with dedupe by eventId,
CoT flush with live-box relocation, anti-OOM guards (giant frame, replay
trip), /stop exactly ONCE, close without pace, and adaptive cadence.

All with injected deps: zero network, zero patched time.sleep.
"""
import unittest
from unittest import mock

import support

from zcode_bot.live_view import (LiveTurnView, COT_TAIL, EVENTS_TRIP,
                                 LIVE_BIG_FRAME, LIVE_FRAME_TRIP)


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _FakeC:
    """Fake ProtoClient: re-sends the COMPLETE replay on every poll
    (the server's real behavior — this is how the dedupe gets exercised).
    F7b: also records the PARAMS (afterSeq) of each request."""

    def __init__(self, evs=None, max_frame=0):
        self.evs = evs or []
        self.max_frame = max_frame
        self.requests = []
        self.pedidos = []  # (method, params)

    def request(self, method, params=None, timeout=None):
        self.requests.append(method)
        self.pedidos.append((method, dict(params or {})))
        return {"result": {"events": [dict(e) for e in self.evs]}}


class _Th:
    """A Thread that lasts `n` laps of the loop."""

    def __init__(self, n=2):
        self.n = n

    def is_alive(self):
        self.n -= 1
        return self.n > 0


def _ev(eid, kind, delta):
    return {"eventId": eid, "payload": {"kind": kind, "delta": delta}}


_TOOL_STREAM = ("tool_input_start", "tool_input_delta", "tool_input_end",
                "tool_call")


def _evr(seq, kind, **payload):
    """Event with the REAL protocol shape (spike F7a): dense seq,
    uuid eventId, envelope type (model.streaming / tool.updated)."""
    p = {"kind": kind}
    p.update(payload)
    tipo = "tool.updated" if kind in ("started", "result", "batch") \
        else "model.streaming"
    return {"eventId": "e-%d" % seq, "seq": seq, "type": tipo, "payload": p}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def _view(self, c, th=None, cfg=None, **dep_extra):
        """View with recorders: sends, edits, live_sends, skips, stops."""
        self.sends = []
        self.edits = []
        self.live_sends = []
        self.skips = []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append(text), True)[1],
            "live_send": lambda cfg, text:
                (self.live_sends.append(text), 550 + len(self.live_sends))[1],
            "live_edit": lambda cfg, mid, text:
                (self.edits.append((mid, text)), True)[1],
            "sleep": lambda s: None,
            "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1,
            "rss_now": lambda: -1,
            "skip_mark": lambda sid: self.skips.append(sid),
        }
        deps.update(dep_extra)
        import time as _t
        return LiveTurnView(c, cfg or _cfg(self.zt), self.T1, _t.time(),
                            "testing", 777, th or _Th(2), last_id=None,
                            deps=deps)


class TestStreaming(_Base):
    async def test_deltas_editan_la_caja_y_el_evento_de_fase(self):
        evs = [_ev(1, "reasoning_delta", "voy a pensar "),
               _ev(2, "text_delta", "escribo la respuesta")]
        v = self._view(_FakeC(evs))
        cot, texto, pasos, live_id = v.run()
        # the think→write phase event sent its own message
        self.assertTrue(any("análisis terminado" in s for s in self.sends))
        # the live box edits with the writing phase
        self.assertTrue(any("escribiendo" in t for _m, t in self.edits))
        # the dedupe by eventId works: the replay was re-sent 2 times but
        # the counters see each delta ONCE
        self.assertEqual(v.n_deltas, 2)
        self.assertTrue(texto.endswith("escribo la respuesta"))
        self.assertEqual(pasos, [])
        # the CLOSE releases ALL the remaining CoT (however small) as a
        # permanent message and the live box moves — the flush contract
        self.assertTrue(any("CoT 1" in s for s in self.sends))
        self.assertNotEqual(live_id, 777)
        self.assertEqual(cot, "")  # delivered to the chat = released

    async def test_pasos_se_acumulan_con_caja_propia(self):
        msgs = [{"info": {"role": "assistant", "id": "m9"}, "parts": [
            {"type": "tool", "tool": "Bash",
             "state": {"input": {"command": ["ls"]}}}]}]
        v = self._view(_FakeC(), fetch_messages=lambda *a, **k: msgs)
        cot, texto, pasos, live_id = v.run()
        self.assertEqual([p for p in pasos], [("Bash", "ls")])
        # the steps box was born (live_send) and got edited with the count
        self.assertTrue(any("pasos:" in s for s in self.live_sends))
        self.assertTrue(any("1 pasos" in t for _m, t in self.edits))


class TestCotFlush(_Base):
    async def test_flush_muda_la_caja_viva_y_el_cierre_manda_el_resto(self):
        evs = [_ev(1, "reasoning_delta", "R" * 5000)]
        v = self._view(_FakeC(evs))
        cot, _texto, _pasos, live_id = v.run()
        # CoT 1 (3000 chars) went out as a permanent message in the loop…
        self.assertTrue(any("CoT 1" in s for s in self.sends))
        # …the live box MOVED at the end (live_send new id ≠ 777)…
        self.assertNotEqual(live_id, 777)
        # …and the close without pace sent the rest as CoT 2
        self.assertTrue(any("CoT 2" in s for s in self.sends))
        # what was delivered to the chat got RELEASED: nothing stays held in memory
        self.assertEqual(cot, "")

    async def test_cot_stream_apagado_no_manda_nada(self):
        evs = [_ev(1, "reasoning_delta", "R" * 5000)]
        v = self._view(_FakeC(evs), cfg=_cfg(self.zt, cot_stream=False))
        cot, _t, _p, live_id = v.run()
        self.assertEqual(self.sends, [])
        self.assertEqual(live_id, 777)  # it never moved
        self.assertEqual(len(cot), 5000)  # the CoT stays for the trace


class TestAntiOOM(_Base):
    """F7+++ — the trips guard the FOLLOWING polls (the first one may
    bring the COMPLETE replay ONCE: it gets paid, shown, released).
    The frame is measured PER POLL (the max_frame inherited from the
    resume was killing healthy streams: 'evs=0 frame=25MB')."""

    async def test_frame_gigante_repetido_apaga_y_marca(self):
        # broken afterSeq: EVERY poll brings 26MB → the first one is
        # tolerated, the SECOND turns it off and marks the session
        from zcode_bot import live_view as zc_live
        with mock.patch.object(zc_live, "_frame_size",
                               lambda r: LIVE_FRAME_TRIP + 1):
            c = _FakeC()
            v = self._view(c, th=_Th(4))
            v.run()
        self.assertTrue(v.events_off)
        self.assertEqual(self.skips, [self.T1])
        self.assertGreaterEqual(c.requests.count("session/events"), 2)

    async def test_frame_gigante_SOLO_en_el_primero_no_apaga(self):
        # startup replay a single time (small incremental afterwards):
        # the CoT is shown IN FULL — no tripping (Damian's request)
        from zcode_bot import live_view as zc_live
        tamaños = [LIVE_FRAME_TRIP + 1, 10, 10]
        with mock.patch.object(zc_live, "_frame_size",
                               lambda r: tamaños.pop(0) if tamaños else 10):
            c = _FakeC()
            v = self._view(c, th=_Th(4))
            v.run()
        self.assertFalse(v.events_off)
        self.assertEqual(self.skips, [])

    async def test_trip_de_replay_apaga_sin_marcar(self):
        # 1300 small events on EVERY poll (replay): the first one is
        # tolerated, the second trips EVENTS_TRIP without marking the session
        evs = [_ev(i, "reasoning_delta", "x") for i in range(EVENTS_TRIP + 100)]
        c = _FakeC(evs)
        v = self._view(c, th=_Th(4))
        v.run()
        self.assertTrue(v.events_off)
        self.assertEqual(self.skips, [])  # it isn't the session: it's this turn
        self.assertGreaterEqual(c.requests.count("session/events"), 2)

    async def test_cadencia_adaptativa_por_frame_grande(self):
        from zcode_bot import live_view as zc_live
        with mock.patch.object(zc_live, "_frame_size",
                               lambda r: LIVE_BIG_FRAME + 1):
            v = self._view(_FakeC(), th=_Th(2))
            v.run()
        self.assertEqual(v.cad, 5.0)  # 2s normal, 5s with a frame >2MB


class TestStop(_Base):
    async def test_stop_pide_session_stop_UNA_vez(self):
        c = _FakeC()
        v = self._view(c, th=_Th(4),
                       stop_requested=lambda sid: True)
        v.run()
        self.assertEqual(c.requests.count("session/stop"), 1)


class TestAfterSeq(_Base):
    """F7b — incremental polling: the request carries afterSeq from the
    start (eventSeq from the subscribe) and the cursor advances with the
    highest seq seen."""

    async def test_cursor_y_afterseq_en_cada_poll(self):
        evs = [_evr(1, "reasoning_delta", delta="pienso "),
               _evr(2, "reasoning_delta", delta="algo"),
               _evr(3, "text_delta", delta="respondo")]
        c = _FakeC(evs)
        v = self._view(c, th=_Th(3))
        v.last_seq = 0  # as if the subscribe had returned eventSeq=0
        v.run()
        polls = [p for m, p in c.pedidos if m == "session/events"]
        self.assertTrue(polls)  # there were polls
        self.assertTrue(all(p.get("afterSeq") == 0 for p in polls[:1]))
        # the cursor stayed at the highest seq seen
        self.assertEqual(v.last_seq, 3)
        # dedupe: the re-sent replay didn't duplicate deltas
        self.assertEqual(v.n_deltas, 3)

    async def test_sin_seq_comporta_como_antes(self):
        # old fixtures without seq: afterSeq is never sent (tolerant)
        evs = [_ev(1, "reasoning_delta", "x")]
        c = _FakeC(evs)
        v = self._view(c, th=_Th(2))
        v.run()
        polls = [p for m, p in c.pedidos if m == "session/events"]
        self.assertTrue(all("afterSeq" not in p for p in polls))


class TestLiveAction(_Base):
    """F7c — a tool's life cycle as seen by the stream: a box that is
    born 🔵 (tool_call, input already parsed), gets edited to ✅/❌ with
    tool.updated.result, and the command is seen being typed in the
    live box."""

    def _tool_evs(self, hasta="result", ok=True):
        evs = [_evr(1, "tool_input_start", toolCallId="c1", toolName="Bash"),
               _evr(2, "tool_input_delta", toolCallId="c1",
                    delta='{"command":"echo ho'),
               _evr(3, "tool_input_delta", toolCallId="c1", delta='la"}'),
               _evr(4, "tool_call", toolCallId="c1", toolName="Bash",
                    input={"command": "echo hola"}),
               _evr(5, "started", toolCallId="c1", toolName="Bash")]
        if hasta == "result":
            evs.append(_evr(6, "result", toolCallId="c1",
                            result={"success": ok, "content": "hola"}))
        elif hasta == "error":
            evs.append(_evr(6, "result", toolCallId="c1",
                            result={"success": False,
                                    "content": "boom"}))
        return evs

    async def test_caja_nace_azul_y_se_edita_a_verde(self):
        v = self._view(_FakeC(self._tool_evs()))
        cot, _t, pasos, _lid = v.run()
        # the permanent box was born with the full command and shows a 🔵
        self.assertTrue(any("echo hola" in s and "🔵" in s
                            for s in self.live_sends))
        # and the result EDITED it to ✅ with the output (box msg_id = 551)
        self.assertTrue(any(m == 551 and "✅" in t and "hola" in t
                            for m, t in self.edits))
        # the steps index has it instantly (stream, not messages)
        self.assertEqual([p.nombre for p in pasos], ["Bash"])

    async def test_result_fallido_marca_rojo(self):
        v = self._view(_FakeC(self._tool_evs(hasta="error")))
        v.run()
        self.assertTrue(any(m == 551 and "❌" in t and "boom" in t
                            for m, t in self.edits))

    async def test_comando_tipeandose_en_la_caja_viva(self):
        # only input_start + one delta: the tool is being typed, no box yet
        evs = [_evr(1, "tool_input_start", toolCallId="c1", toolName="Bash"),
               _evr(2, "tool_input_delta", toolCallId="c1",
                    delta='{"command":"git sta')]
        v = self._view(_FakeC(evs))
        v.run()
        self.assertTrue(any("tipeando" in t and "git sta" in t
                            for _m, t in self.edits))
        # without tool_call no permanent box was born
        self.assertFalse(any("Bash" in s for s in self.live_sends))

    async def test_tools_stream_off_apaga_el_live_action(self):
        v = self._view(_FakeC(self._tool_evs()),
                       cfg=_cfg(self.zt, tools_stream=False))
        cot, _t, pasos, _lid = v.run()
        self.assertFalse(any("Bash" in s for s in self.live_sends))
        self.assertEqual(pasos, [])

    async def test_read_solo_indice_con_preset_accion(self):
        # F7c+: 'accion' (default) — Read doesn't spend a permanent box...
        evs = [_evr(1, "tool_call", toolCallId="c1", toolName="Read",
                    input={"file_path": "/x/y.py"})]
        v = self._view(_FakeC(evs))
        _cot, _t, pasos, _lid = v.run()
        self.assertFalse(any("Read" in s for s in self.live_sends))
        self.assertEqual([p.nombre for p in pasos], ["Read"])  # the index does
        # ...but with tools_caja_filtro=todas the box is born
        v2 = self._view(_FakeC(evs),
                        cfg=_cfg(self.zt, tools_caja_filtro="todas"))
        v2.run()
        self.assertTrue(any("Read" in s for s in self.live_sends))

    async def test_el_mensaje_no_duplica_lo_del_stream(self):
        # the history brings the SAME tool (callID c1): the index isn't
        # duplicated and the box isn't re-sent (boxed)
        cajas = []
        msgs = [{"info": {"role": "assistant", "id": "m9"}, "parts": [
            {"type": "tool", "tool": "Bash", "callID": "c1",
             "state": {"input": {"command": "echo hola"},
                       "status": "completed", "output": "hola"}}]}]
        v = self._view(_FakeC(self._tool_evs()),
                       fetch_messages=lambda *a, **k: msgs,
                       cajas_paso=lambda cfg, t, cont, nuevos, boxed=None:
                       cajas.append((nuevos, boxed)))
        cot, _t, pasos, _lid = v.run()
        self.assertEqual([p.nombre for p in pasos], ["Bash"])  # ONCE
        # the fallback received the tool with its callID and the boxed set
        self.assertEqual(cajas[0][0][0][0], "c1")
        self.assertIn("c1", cajas[0][1])

    async def test_caja_azul_cerrada_por_el_historial_si_falta_result(self):
        # the stream died midway (events_off/disconnect): the 🔵 box is
        # closed by the messages poll with the output from the history
        msgs = [{"info": {"role": "assistant", "id": "m9"}, "parts": [
            {"type": "tool", "tool": "Bash", "callID": "c1",
             "state": {"input": {"command": "echo hola"},
                       "status": "completed", "output": "hola"}}]}]
        v = self._view(_FakeC(self._tool_evs(hasta="started")),
                       fetch_messages=lambda *a, **k: msgs)
        v.run()
        self.assertTrue(any(m == 551 and "✅" in t and "hola" in t
                            for m, t in self.edits))


if __name__ == "__main__":
    unittest.main()
