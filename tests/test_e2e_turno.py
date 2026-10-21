# -*- coding: utf-8 -*-
"""E2E — el viaje COMPLETO de un turno, sin tramos parcheados al medio.

Nacieron del escepticismo sano de Damian post-F49 (20/10: «vos decís que
funciona? hace varios tests e2e y fijate que todo el bot funciona bien»):
la suite cubría los tramos POR SEPARADO (el happy-path del worker con el
LiveTurnView de mentira, el LiveTurnView con el gateway de mentira) y el
viaje entero solo se probaba en vivo. La lección grabada: verificar estado
interno no alcanza para UX de chat — hay que assertar la SECUENCIA de
mensajes que el dueño ve.

Falsificado (los bordes del sistema, nada del medio):
  - el runtime zcode: un ProtoClient de mentira que entrega los eventos
    del turno por tandas (una por poll), honrando afterSeq de verdad;
  - Telegram: un chat fake que graba cada send/edit en orden cronológico
    y reparte message_ids crecientes (lo que ph_de necesita: ints).

TODO lo demás es el código de producción: prompt_worker (spawn, proto,
turno, entrega, finally), LiveTurnView REAL (sin deps inyectadas), el
sellador, razonamiento (cache + log_teclado → pwlog del fixture), el
gateway y el despacho de comandos.

E1  turno CoT multi-razonada — el escenario del bug de los botones: cada
    razonada en SU mensaje con SUS botones, cierre sin índice global
E2  taps sobre el cache que dejó E1 — el visor edita el mensaje justo
    con los botones que VIVEN en él (regresión F48b/F49)
E3  turno sin CoT — el flujo clásico intacto + el aviso «sin CoT» (F45)
E4  comandos del día a día por process_update_sync, despacho real
E5  rezagadas — el send de la 2ª razonada falla → UN mensaje de tardías
"""
import glob
import json
import os
import threading
import time
import unittest
from unittest import mock

import support

from zcode_bot import workers as zc_workers
from zcode_bot.core import set_active_bot_session
from zcode_bot.utils import http as zc_http


# ------------------------------------------------------------- fakes

def _con_seqs(tandas):
    """[[ (kind, delta), ... ], ...] → una lista de EVENTOS por tanda, con
    seq/eventId incrementales desde el 101 (el 100 es el eventSeq que
    devuelve el subscribe del fake)."""
    seq = 100
    out = []
    for tanda in tandas:
        evs = []
        for kind, delta in tanda:
            seq += 1
            p = {"kind": kind}
            if delta:
                p["delta"] = delta
            evs.append({"seq": seq, "eventId": "e%d" % seq, "payload": p})
        out.append(evs)
    return out


class FakeRuntime:
    """ProtoClient de mentira: el turno completo visto por el stream.

    session/events entrega UNA tanda por llamada (el LiveTurnView pollea
    cada ~2 s); cuando se agotan, el primer poll VACÍO suelta `listo` —
    así el _turn_end_poll fake (que espera `listo`) recién mata el th
    cuando la vista ya PROCESÓ la última tanda: sin carreras, sin eventos
    tragados."""

    def __init__(self, tandas, respuesta="respuesta final del turno"):
        self.tandas = _con_seqs(tandas)
        self.respuesta = respuesta
        self.requests = []
        self.closed = False
        self.listo = threading.Event()

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        if method == "session/events":
            after = (params or {}).get("afterSeq")
            if self.tandas:
                tanda = self.tandas.pop(0)
                evs = [e for e in tanda
                       if after is None or e["seq"] > after]
            else:
                self.listo.set()
                evs = []
            return {"result": {"events": evs}}
        if method == "session/messages":
            return {"result": {"messages": [
                {"info": {"role": "user", "id": "m1"},
                 "parts": [{"type": "text", "text": "hacé algo"}]},
                {"info": {"role": "assistant", "id": "m2"},
                 "parts": [{"type": "text", "text": self.respuesta}]}]}}
        if method == "session/subscribe":
            return {"result": {"eventSeq": 100}}
        return {"result": {}}

    def close(self):
        self.closed = True


class ChatTelegram:
    """Telegram de mentira: graba cada send/edit EN ORDEN (lo que el dueño
    ve) con su message_id y markup. `marca_resultados` programa qué
    devolver en los sends CON markup (E5: None = el send falló)."""

    def __init__(self):
        self.log = []            # ("send"|"edit", mid, texto, markup)
        self._mid = 699
        self.marca_resultados = []

    def _next(self):
        self._mid += 1
        return self._mid

    def send(self, cfg, text, loud=None, markup=None):
        mid = self._next()
        self.log.append(("send", mid, text, markup))
        if markup is not None and self.marca_resultados:
            r = self.marca_resultados.pop(0)
            return mid if r is True else r
        return mid

    def live_send(self, cfg, text):
        mid = self._next()
        self.log.append(("send", mid, text, None))
        return mid

    def live_edit(self, cfg, mid, text, markup=None):
        self.log.append(("edit", mid, text, markup))
        return True

    def sends(self):
        return [e for e in self.log if e[0] == "send"]

    def edits(self):
        return [e for e in self.log if e[0] == "edit"]

    @staticmethod
    def botones(e):
        kb = e[3] or {}
        return [b["text"] for fila in kb.get("inline_keyboard", [])
                for b in fila]


# El escenario de Damian: 2 razonadas (3 cajas en la 1ª, 1 en la 2ª),
# una tool al medio y texto al final — el turno que acumulaba botones.
TANDAS_2RAZ = [
    [("reasoning_delta", "x" * 2000), ("reasoning_delta", "y" * 1500)],
    [("tool_input_start", ""), ("tool_call", "")],
    [("reasoning_delta", "z" * 300)],
    [("text_delta", "escribo la respuesta")],
]
# x2000 + y1500 = 3500 → caja 1 (3000) llena EN VIVO; la cola (y*500) se
# sella al cerrar r1 por la tool; r2 (z*300) cierra al escribir → 2.1.


class _E2EBase(unittest.TestCase):
    """Entorno fresh + Telegram/runtime falsos. El worker, la vista, el
    sellador, razonamiento y el gateway corren DE VERDAD."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.chat = ChatTelegram()
        for mod, name, val in (
            (zc_http, "send", self.chat.send),
            (zc_http, "_live_send", self.chat.live_send),
            (zc_http, "_live_edit", self.chat.live_edit),
            (self.zt, "send", self.chat.send),      # el reply del gateway
            (self.zt, "react", lambda cfg, msg, e: None),
            (self.zt, "typing", lambda cfg: None),
        ):
            p = mock.patch.object(mod, name, val)
            p.start()
            self.addCleanup(p.stop)

    def _turno(self, tandas, respuesta="respuesta final del turno",
               **cfg_extra):
        """Corre prompt_worker REAL sobre el FakeRuntime; devuelve el
        runtime (para inspeccionar requests/close)."""
        runtime = FakeRuntime([list(t) for t in tandas], respuesta)

        def _fin(sid, t0, **kw):
            runtime.listo.wait(30)   # la vista ya procesó la última tanda
            time.sleep(0.05)         # y completed su último poll
            return {"event": "turn.completed"}

        patches = [
            mock.patch.object(zc_workers, "zc_proto", type("M", (), {
                "make": staticmethod(lambda cwd=None, **kw: runtime),
                "proto_err": __import__(
                    "zcode_bot.proto", fromlist=["proto_err"]).proto_err})),
            mock.patch.object(zc_workers, "_turn_end_poll", _fin),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1)
        cfg.update(cfg_extra)
        # el fixture apaga cot_botones para la suite entera: acá es el
        # default del bot de producción (cada test puede apagarlo)
        cfg["cot_botones"] = cfg_extra.get("cot_botones", True)
        # T2: su turno del fixture está COMPLETADO (T1 tiene un
        # turn.started abierto → running_turns la da ocupada → busy).
        # Y la declaramos activa, como haría un /task n real — el filtro
        # only_active_task silencia el stream de cualquier otra.
        act = {"sid": support.T2, "title": "CV", "ws": support.PROJ_CV}
        set_active_bot_session(act)
        zc_workers.prompt_worker(cfg, act, "hacé algo con varias tools")
        return runtime

    def _pwlog(self):
        p = os.path.join(self.zt.config.BASE, "hooks", "logs",
                         "prompt-worker.log")
        with open(p) as f:
            return f.read()

    def _cache(self):
        """El (dict, t0) del cache del turno — sin adivinar t0: del disco."""
        pat = os.path.join(self.zt.config.STATE_DIR,
                           "cot-%s-*.json" % support.T2)
        archs = glob.glob(pat)
        self.assertEqual(len(archs), 1, "un cache por turno: %s" % archs)
        pref = "cot-%s-" % support.T2
        t0 = int(os.path.basename(archs[0])[len(pref):-len(".json")])
        with open(archs[0]) as f:
            return json.load(f), t0


# ----------------------------------------------------------------- E1

class TestE2ETurnoCoT(_E2EBase):
    """E1 — el turno del bug: dos razonadas, cada una en SU mensaje con
    SUS botones, y el cierre SIN índice global (F46+F48c+F49 juntos)."""

    def test_cada_razonada_su_mensaje_sus_botones_y_cierre_limpio(self):
        runtime = self._turno(TANDAS_2RAZ)
        chat = self.chat

        # --- la respuesta final del runtime llegó al chat
        self.assertTrue(any("respuesta final del turno" in s[2]
                            for s in chat.sends()))

        # --- nace r1: SU mensaje, SU único botón
        nace1 = [s for s in chat.sends()
                 if chat.botones(s) == ["CoT 1.1"]]
        self.assertEqual(len(nace1), 1)
        ph1 = nace1[0][1]
        self.assertIn("1 caja", nace1[0][2])

        # --- cierre r1: el ÚLTIMO edit de ph1 con SUS dos botones
        edits1 = [e for e in chat.edits() if e[1] == ph1]
        self.assertTrue(edits1)
        self.assertIn("razonada 1", edits1[-1][2])
        self.assertEqual(chat.botones(edits1[-1]), ["CoT 1.1", "CoT 1.2"])

        # --- nace r2: OTRO mensaje, SOLO su botón (la regresión: el bug
        #     acumulaba TODOS los botones en la última caja)
        nace2 = [s for s in chat.sends() if chat.botones(s) == ["CoT 2.1"]]
        self.assertEqual(len(nace2), 1)
        ph2 = nace2[0][1]
        self.assertNotEqual(ph2, ph1)
        edits2 = [e for e in chat.edits() if e[1] == ph2]
        self.assertIn("razonada 2", edits2[-1][2])
        self.assertEqual(chat.botones(edits2[-1]), ["CoT 2.1"])

        # --- NINGÚN mensaje del chat mezcla botones de dos razonadas
        #     (el índice global de F46/F48b se fue de verdad)
        for e in chat.log:
            b = chat.botones(e)
            self.assertFalse(
                "CoT 1.1" in b and "CoT 2.1" in b,
                "acumulación en %r: %r" % (e[0], b))
        # --- y el texto_final del índice tampoco volvió
        self.assertFalse(any("Razonamiento de «" in e[2] for e in chat.log))

        # --- el cache quedó finalizado con la cronología de mensajes
        d, _t0 = self._cache()
        self.assertIs(d.get("vivo"), None)
        self.assertTrue(d.get("finalizado"))
        self.assertEqual(d.get("rotulos"),
                         ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(d.get("phs"), [ph1, ph1, ph2])

        # --- el loop de feedback F49: el pwlog reconstruye el teclado
        #     que quedó mostrando CADA mensaje
        pw = self._pwlog()
        self.assertIn("cot kb celu nace r1 ph=%d [1]: CoT 1.1" % ph1, pw)
        self.assertIn("cot kb celu cierre r1 ph=%d [2]: CoT 1.1 CoT 1.2"
                      % ph1, pw)
        self.assertIn("cot kb celu nace r2 ph=%d [1]: CoT 2.1" % ph2, pw)
        self.assertIn("cot kb celu cierre r2 ph=%d [1]: CoT 2.1" % ph2, pw)
        self.assertRegex(pw, r"live fin: .*cot_msgs=2 .*cajas=3")

        # --- el worker limpió TODO al terminar
        self.assertIsNone(self.zt._workers().get(support.T2))
        self.assertTrue(runtime.closed)


# ----------------------------------------------------------------- E2

class TestE2ETapsSobreCacheReal(_E2EBase):
    """E2 — los taps disparan /cotb con el cache que dejó un turno REAL
    (no un json a mano): el visor edita el mensaje donde VIVE la caja,
    con los botones de ESE mensaje (F49)."""

    def test_el_tap_edita_el_mensaje_justo_con_sus_botones(self):
        self._turno(TANDAS_2RAZ)
        d, t0 = self._cache()
        ph1, ph2 = d["phs"][0], d["phs"][2]
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                   owner_user_id=9, enabled=True)
        antes = len(self.chat.log)

        def tap(n):
            self.zt.process_update_sync(cfg, {
                "chat": {"id": 1}, "from": {"id": 9},
                "text": "/cotb %s %d %d" % (support.T2, t0, n),
                "_cb_data": "ct:%s:%d:%d" % (support.T2, t0, n)})

        # tap la caja 1 (vive en ph1): edita ph1 con 1.1 y 1.2 — sin 2.1
        tap(1)
        nuevos = self.chat.log[antes:]
        self.assertEqual([e[0] for e in nuevos], ["edit"])
        self.assertEqual(nuevos[0][1], ph1)
        b = self.chat.botones(nuevos[0])
        self.assertIn("CoT 1.1", b)
        self.assertIn("CoT 1.2", b)
        self.assertNotIn("CoT 2.1", b)     # la regresión de F48b
        self.assertIn("x" * 100, nuevos[0][2])   # el CONTENIDO de la caja 1
        antes = len(self.chat.log)

        # tap la caja 3 (vive en ph2): edita ph2 con SOLO 2.1
        tap(3)
        nuevos = self.chat.log[antes:]
        self.assertEqual([e[0] for e in nuevos], ["edit"])
        self.assertEqual(nuevos[0][1], ph2)
        b = self.chat.botones(nuevos[0])
        self.assertIn("CoT 2.1", b)
        self.assertNotIn("CoT 1.1", b)
        self.assertIn("z" * 100, nuevos[0][2])

        # nada nuevo abajo, ningún cartel — y el pwlog registró ambos
        pw = self._pwlog()
        self.assertIn("cot kb cotb n=1 ph=%d" % ph1, pw)
        self.assertIn("cot kb cotb n=3 ph=%d" % ph2, pw)


# ----------------------------------------------------------------- E3

class TestE2ESinCoT(_E2EBase):
    """E3 — el flujo clásico no se rompió: sin razonamiento no hay cajas
    ni índice, la respuesta llega igual (y F45 lo dice con una línea)."""

    TANDAS_TEXTO = [[("text_delta", "hola ")],
                    [("text_delta", "mundo")]]

    def test_sin_botones_la_respuesta_llega_y_la_viva_cierra(self):
        self._turno(self.TANDAS_TEXTO, cot_botones=False)
        chat = self.chat
        # sin CoT no nace NINGÚN mensaje con botones de cajas
        self.assertFalse(any(chat.botones(s) for s in chat.sends()))
        # la respuesta final sí llegó
        self.assertTrue(any("respuesta final del turno" in s[2]
                            for s in chat.sends()))
        # la caja viva cerró con ✅
        self.assertTrue(any(e[0] == "edit" and "✅" in e[2]
                            for e in chat.edits()))
        self.assertRegex(self._pwlog(), r"live fin: .*cajas=0")

    def test_con_botones_un_turno_sin_razonamiento_lo_dice(self):
        # F45: sin el aviso, cero cajitas era indistinguible de un bug
        self._turno(self.TANDAS_TEXTO, cot_botones=True)
        aviso = [s for s in self.chat.sends() if "sin CoT" in s[2]]
        self.assertEqual(len(aviso), 1)
        self.assertIn("«CV»", aviso[0][2])

    def test_modo_stream_el_cot_de_texto_clasico_sigue_vivo(self):
        # cot_stream sin cot_botones (el modo pre-F42): el CoT llega como
        # MENSAJES de texto permanentes «CoT n», el aviso de fase es UNO,
        # y ningún botón — lo destapó este mismo e2e cuando el default
        # del cfg quedó apagado por accidente: anduvo igual que siempre.
        self._turno(TANDAS_2RAZ, cot_botones=False)
        chat = self.chat
        cot_msg = [s for s in chat.sends() if "CoT 1" in s[2]]
        self.assertTrue(cot_msg)
        self.assertTrue(any("CoT 2" in s[2] for s in chat.sends()))
        self.assertFalse(any(chat.botones(s) for s in chat.sends()))
        fase = [s for s in chat.sends() if "análisis terminado" in s[2]]
        self.assertEqual(len(fase), 1)      # UN aviso de fase por turno
        self.assertTrue(any("respuesta final del turno" in s[2]
                            for s in chat.sends()))


# ----------------------------------------------------------------- E4

class TestE2EComandos(_E2EBase):
    """E4 — los comandos del día a día por el gateway con despacho REAL:
    cada uno responde algo sensato, nada explota, el desconocido avisa."""

    def setUp(self):
        super().setUp()
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        owner_user_id=9, enabled=True)

    def _update(self, text, cb=None):
        m = {"chat": {"id": 1}, "from": {"id": 9}, "text": text}
        if cb:
            m["_cb_data"] = cb
        self.zt.process_update_sync(self.cfg, m)

    def test_la_bateria_del_dia_a_dia(self):
        casos = [
            ("/help", ["status", "task"]),        # el menú entero
            ("/status", None),
            ("/task", ["testing"]),               # la lista del fixture
            ("/all_tasks", ["CV"]),
            ("/model", None),
            ("/ping", ["pong"]),
            ("/last", ["Última respuesta de ejemplo"]),
            ("/cotb", ["Uso"]),                   # sin args: cómo se usa
        ]
        for text, esperados in casos:
            n = len(self.chat.log)
            self._update(text)
            nuevos = [e for e in self.chat.log[n:] if e[0] == "send"]
            self.assertTrue(nuevos, "sin respuesta para %s" % text)
            cuerpo = "\n".join(e[2] for e in nuevos)
            for esp in esperados or []:
                self.assertIn(esp, cuerpo, "%s → %r" % (text, cuerpo))

        # el inexistente de verdad sigue avisando (F48a intacto)
        self._update("/noexiste")
        self.assertTrue(any("Comando desconocido" in e[2]
                            for e in self.chat.sends()))

        # y un botón de pregunta vencida no inyecta nada
        self._update("si", cb="yes")
        self.assertTrue(any("ya venció" in e[2] for e in self.chat.sends()))

        # TODO el chat sin errores internos
        self.assertFalse(any("Error interno" in e[2] for e in self.chat.log))


# ----------------------------------------------------------------- E5

class TestE2ERezagadas(_E2EBase):
    """E5 — el send de la 2ª razonada falla (sin message_id): su caja no
    se pierde — nace UN mensaje de tardías al cierre con ESA caja, y la
    1ª razonada queda intacta con sus botones (F49)."""

    def test_la_caja_sin_mensaje_va_en_tardias_y_no_contamina(self):
        self.chat.marca_resultados = [True, None]   # nace r2 falla
        self._turno(TANDAS_2RAZ)
        chat = self.chat

        # la 1ª razonada viva igual: SU mensaje con SUS dos botones
        nace1 = [s for s in chat.sends()
                 if chat.botones(s) == ["CoT 1.1"]]
        self.assertEqual(len(nace1), 1)
        ph1 = nace1[0][1]
        edits1 = [e for e in chat.edits() if e[1] == ph1]
        self.assertEqual(chat.botones(edits1[-1]), ["CoT 1.1", "CoT 1.2"])

        # UN mensaje de tardías con SOLO la caja huérfana
        tardias = [s for s in chat.sends()
                   if "al cierre del turno" in s[2]]
        self.assertEqual(len(tardias), 1)
        self.assertEqual(chat.botones(tardias[0]), ["CoT 2.1"])
        ph_t = tardias[0][1]
        self.assertNotEqual(ph_t, ph1)

        # el cache: la caja 3 vive en el mensaje de tardías
        d, _t0 = self._cache()
        self.assertEqual(d.get("phs"), [ph1, ph1, ph_t])

        # el pwlog lo dice, y ningún mensaje mezcla razonadas
        self.assertIn("cot kb celu tardias ph=%d [1]: CoT 2.1" % ph_t,
                      self._pwlog())
        self.assertRegex(self._pwlog(), r"live fin: .*cot_msgs=2 .*cajas=3")
        for e in chat.log:
            b = chat.botones(e)
            self.assertFalse("CoT 1.1" in b and "CoT 2.1" in b)


if __name__ == "__main__":
    unittest.main()
