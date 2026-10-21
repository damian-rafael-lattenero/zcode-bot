# -*- coding: utf-8 -*-
"""F50 — fixes from the deep audit of 20/10 («read everything, hunt for
errors everywhere and fix them»).

Four agents + automated tools (pyflakes, import AST) read the whole
package. The bugs fixed here:

1. truncar_html (utils/formatting): send/_live_send/_live_edit truncated
   the ALREADY-built HTML at 3900 chars — the cut could land on an open
   <pre> (its closing tag lives at the end) and Telegram rejected the
   WHOLE message with 400 «Unclosed tag»: lost + the outbox retrying it
   forever. The inflation came from esc() (&→5x, <>→4x) over caps
   measured raw.
2. _partes_por_escape (reporting): _send_long_reply split at 3900 RAW
   and escaped afterwards — a part could inflate to ~5x and send
   truncated it: answer silently cut with the «(2/3)» lying.
3. BotSession.mode_set (core): prompt_worker wrote it after setMode
   but from_dict DISCARDED it — the skip_mode optimization was dead
   and every cold turn paid session/setMode again.
4. Atomic save_config: crash mid json.dump → truncated file →
   load_config fell to DEFAULTS (empty token) → deaf bot, no warning.
5. Answering «0» to a question: a negative index picked the
   LAST option out of the blue.
6. /cotb with n=0: phs[-1] pointed at the last reasoning.
7. read_quota_cache: max() over empty limits blew up /quota.
"""
import json
import os
import unittest
from unittest import mock

import support

from zcode_bot import config as zc_config
from zcode_bot.utils.formatting import esc, truncar_html
from zcode_bot import limites as lim


class TestTruncarHtml(unittest.TestCase):
    def test_corto_intacto(self):
        self.assertEqual(truncar_html("<b>hola</b>", 100), "<b>hola</b>")

    def test_pre_abierto_se_cierra(self):
        # the case of an HTML /diff or a CoT box with many escapes
        texto = "<pre>" + "x" * 8000 + "</pre>"
        t = truncar_html(texto, 3900)
        self.assertLessEqual(len(t), 3900 + len("</pre>"))
        self.assertTrue(t.startswith("<pre>"))
        self.assertTrue(t.endswith("</pre>"))  # Telegram accepts it whole

    def test_entidad_partida_por_el_corte(self):
        texto = "a" * 3980 + " &amp; y sigue"
        t = truncar_html(texto, 3900)
        self.assertFalse(t.endswith("&am"))    # broken entity, out
        self.assertNotIn("&am;", t)

    def test_tag_partido_por_el_corte(self):
        texto = "a" * 3899 + "<pre>resto"
        t = truncar_html(texto, 3900)
        self.assertFalse(t.rstrip().endswith("<"))  # stray <pr, out

    def test_anidados_se_cierran_en_orden(self):
        texto = "<b>negrita <pre>%s</pre></b>" % ("y" * 9000)
        t = truncar_html(texto, 40)
        self.assertTrue(t.endswith("</pre></b>"))


class TestSendTruncaSeguro(unittest.TestCase):
    """send() uses truncar_html: whatever reaches Telegram always parses."""

    def setUp(self):
        self.zt = support.fresh(self)
        from zcode_bot.utils import http as zc_http
        self.params = []
        p = mock.patch.object(zc_http, "tg_api",
                              lambda cfg, method, params=None, timeout=35:
                              (self.params.append(params),
                               {"result": {"message_id": 1}})[1])
        p.start()
        self.addCleanup(p.stop)
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False)

    def test_el_texto_que_viaja_esta_balanceado_y_bajo_4096(self):
        from zcode_bot.utils import http as zc_http
        texto = "<pre>%s</pre>" % ("&<>" * 5000)   # esc() inflates 5x
        zc_http.send(self.cfg, texto)
        t = self.params[0]["text"]
        self.assertLessEqual(len(t), 4096)
        self.assertTrue(t.endswith("</pre>"))
        # no broken entities
        self.assertNotIn("&am;", t.replace("&amp;", ""))
        self.assertNotIn("&l", t.replace("&lt;", ""))


class TestPartesPorEscape(unittest.TestCase):
    def test_texto_inflado_parte_completo(self):
        from zcode_bot.reporting import _partes_por_escape
        cap = lim.CH_RESPUESTA - 300
        texto = "&" * 20000           # esc() inflates it 5x
        partes = _partes_por_escape(texto, cap)
        self.assertGreater(len(partes), 1)
        for p in partes:
            self.assertLessEqual(len(esc(p)), cap)
        self.assertEqual("".join(partes), texto)   # nothing is lost

    def test_texto_normal_una_parte(self):
        from zcode_bot.reporting import _partes_por_escape
        cap = lim.CH_RESPUESTA - 300
        texto = "respuesta corta del turno"
        self.assertEqual(_partes_por_escape(texto, cap), [texto])


class TestModeSetRoundTrip(unittest.TestCase):
    def test_sobrevive_el_guardado_y_la_lectura(self):
        zt = support.fresh(self)
        from zcode_bot.core import (set_active_bot_session,
                                    active_bot_session)
        set_active_bot_session({"sid": "s1", "title": "t", "ws": "/w",
                                "mode_set": "yolo"})
        self.assertEqual(active_bot_session().get("mode_set"), "yolo")
        # without mode_set it doesn't appear (historical shape preserved)
        set_active_bot_session({"sid": "s2", "title": "t", "ws": "/w"})
        with open(os.path.join(zt.config.STATE_DIR,
                               zc_config.ARCH_SESION)) as f:
            self.assertNotIn("mode_set", json.load(f))


class TestSaveConfigAtomico(unittest.TestCase):
    def test_tmp_reemplaza_y_no_deja_basura(self):
        zt = support.fresh(self)
        zt.save_config({"token": "abc", "enabled": True, "chat_id": 5})
        path = zt.config.CONFIG_PATH
        with open(path) as f:
            self.assertEqual(json.load(f)["token"], "abc")
        sobras = [p for p in os.listdir(os.path.dirname(path))
                  if ".tmp." in p]
        self.assertEqual(sobras, [])


class TestRespuestaCeroYPregunta(unittest.TestCase):
    def test_cero_no_selecciona_la_ultima_opcion(self):
        zt = support.fresh(self)
        from zcode_bot.commands import dispatch as zc_dispatch
        from zcode_bot.core import set_active_bot_session, _questions
        sid = support.T2
        set_active_bot_session({"sid": sid, "title": "CV",
                                "ws": support.PROJ_CV})
        _questions().ask(sid, "req-1", "¿Seguimos?",
                         [{"label": "Sí", "value": "si"},
                          {"label": "No", "value": "no"}])
        try:
            r = zc_dispatch.handle_free_text(
                dict(zt.DEFAULTS, token="T", chat_id=1), "0")
            # the "0" travels AS TEXT (the keyboard numbers from 1); with
            # the negative-index bug it answered "no" (the LAST option)
            self.assertIn("Answered: 0", r)
            self.assertNotIn("Respondido: no", r)
        finally:
            _questions().answer("req-1", None)   # clears the question


class TestCotbCero(unittest.TestCase):
    def test_n_cero_no_edita_la_ultima_caja(self):
        zt = support.fresh(self)
        from zcode_bot import razonamiento as rz
        from zcode_bot.utils import http as zc_http
        sid, t0 = support.T1, 1760000000
        os.makedirs(os.path.dirname(rz.cache_path(sid, t0)), exist_ok=True)
        with open(rz.cache_path(sid, t0), "w") as f:
            json.dump({"sid": sid, "titulo": "t", "t0": t0,
                       "cajas": ["uno", "dos"], "rotulos": ["CoT 1.1",
                                                            "CoT 1.2"],
                       "ph": 555, "phs": [111, 222]}, f)
        edits = []
        p = mock.patch.object(zc_http, "_live_edit",
                              lambda cfg, ph, texto, markup=None:
                              (edits.append(ph), True)[1])
        p.start()
        self.addCleanup(p.stop)
        # n=0: out of range — shown as a notice over the GLOBAL ph
        r = zt.handle_command(
            dict(zt.DEFAULTS, token="T", chat_id=1),
            "/cotb %s %d 0" % (sid, t0))
        self.assertEqual(edits, [555])       # the global one, NOT 222
        self.assertNotEqual(edits, [222])


class TestColaHonesta(unittest.TestCase):
    """(H) An expired flock swallows nothing more: the owner finds out and
    /qgo doesn't duplicate by launching without dequeuing the item."""

    def setUp(self):
        self.zt = support.fresh(self)

    def _cola_trabada(self, mod):
        """A _store whose flock always expires — ONLY in the given module
        (the registry uses update_cross_process for other things)."""

        class _StoreTrabado:
            def __init__(self, *a, **k):
                pass

            def update_cross_process(self, fn, default=None):
                raise OSError("flock deadline")

        p = mock.patch.object(mod, "_store", _StoreTrabado)
        p.start()
        self.addCleanup(p.stop)

    def test_put_fallido_avisa_y_no_miente(self):
        from zcode_bot.commands import handlers_tareas as ht
        from zcode_bot import workers as zw
        from zcode_bot.core import set_active_bot_session
        sid = support.T2
        set_active_bot_session({"sid": sid, "title": "CV",
                                "ws": support.PROJ_CV})
        self._cola_trabada(zw)   # _queue_put resolves _store in workers
        with mock.patch.object(ht, "running_turns", lambda: {sid}):
            r = ht.handle_free_text(
                dict(self.zt.DEFAULTS, token="T", chat_id=1), "hacé algo")
        self.assertIn("NOT queue your message", r)  # the honest notice

    def test_qgo_con_cola_trabada_no_lanza_nada(self):
        from zcode_bot.commands import handlers_tareas as ht
        from zcode_bot import workers as zw
        sid = support.T2
        zw._queue_put(sid, {"sid": sid, "title": "CV",
                            "ws": support.PROJ_CV}, "prompt encolado")
        spawns = []
        with mock.patch.object(ht, "_spawn_worker",
                               lambda cfg, act, text:
                               (spawns.append(text), 777)[1]):
            self._cola_trabada(ht)
            r = ht._cmd_qgo(dict(self.zt.DEFAULTS, token="T", chat_id=1),
                            "%s:0" % sid)
        self.assertIn("stuck", r)
        self.assertEqual(spawns, [])            # nothing launched: no duplicate


class TestQuotaLimitsVacios(unittest.TestCase):
    def test_limits_vacio_no_revienta(self):
        zt = support.fresh(self)
        from zcode_bot import reporting
        # fake leveldb in the fixture (the real path is absolute:
        # a conditional expanduser redirects it here)
        root = zt.config.BASE
        ldir = os.path.join(root, "leveldb")
        os.makedirs(ldir, exist_ok=True)
        with open(os.path.join(ldir, "000003.log"), "wb") as f:
            f.write(b'garbage "quota":{"level":"X","limits":[]} trash')
        with open(os.path.join(ldir, "000004.log"), "wb") as f:
            f.write(b'"quota":{"level":"X","limits":'
                    b'[{"type":"CREDIT_LIMIT","nextResetTime":99,'
                    b'"number":5,"currentValue":1,"usage":10,'
                    b'"remaining":9,"percentage":10}]}')
        real = os.path.expanduser

        def fake_exp(p):
            if p.startswith("~/Library/Application Support/ZCode"):
                return ldir   # the fixture's fake leveldb
            return real(p)
        with mock.patch("os.path.expanduser", fake_exp):
            r = reporting.read_quota_cache()
        self.assertIsNotNone(r)   # the healthy one won, no ValueError
        self.assertEqual(r["windows"][0]["hours"], 5)


class TestCarreraDeRegister(unittest.TestCase):
    """(agent workers/live_view #1) register()'s return value is the
    anti-duplication signal: losing the race to another spawn must
    ENQUEUE, not run a second turn on the same session."""

    def test_perder_la_carrera_encola_y_no_arranca(self):
        zt = support.fresh(self)
        from zcode_bot import workers as zw

        class _RegistryCiego:
            """get() doesn't see the other worker the FIRST time (the race) —
            register() returns False because it's ALREADY registered."""
            def __init__(self, real):
                self.real = real
                self.veces = 0

            def get(self, sid):
                self.veces += 1
                return None if self.veces == 1 else self.real.get(sid)

            def __getattr__(self, n):
                return getattr(self.real, n)

        reg = zt._workers()
        # a worker from ANOTHER pid already registered the session
        reg.register(support.T2, {"t0": 1, "pid": 999999,
                                  "titulo": "CV", "prompt": "x"})
        encolados = []
        with mock.patch.object(zw, "_workers",
                               lambda: _RegistryCiego(reg)), \
             mock.patch.object(zw, "_queue_put",
                               lambda s, a, t: encolados.append(t) or True), \
             mock.patch.object(zw, "zc_proto",
                               type("M", (), {"make": staticmethod(
                                   lambda **k: (_ for _ in ()).throw(
                                       AssertionError("no debió llegar "
                                                      "al runtime")))})):
            zw.prompt_worker(dict(zt.DEFAULTS, token="T", chat_id=1),
                             {"sid": support.T2, "title": "CV",
                              "ws": support.PROJ_CV}, "mensaje de carrera")
        self.assertEqual(encolados, ["mensaje de carrera"])


class TestFifoDeSpawnFallido(unittest.TestCase):
    """(agent #2) The item whose spawn failed goes back to the FRONT —
    putting it at the end reversed the order of the user's messages."""

    def test_spawn_fallido_deja_la_cola_en_orden(self):
        zt = support.fresh(self)
        from zcode_bot import workers as zw
        sid = support.T2
        for txt in ("A", "B", "C"):
            zw._queue_put(sid, {"sid": sid, "title": "CV",
                                "ws": support.PROJ_CV}, txt)
        cfg = dict(zt.DEFAULTS, token="T", chat_id=1)
        with mock.patch.object(zw, "_spawn_worker", lambda *a: None):
            r = zw._queue_launch_next(cfg, sid)
        self.assertIsNone(r)
        self.assertEqual([i["text"] for i in zw._queue_take(sid)],
                         ["A", "B", "C"])   # A still first


class TestStopCombinado(unittest.TestCase):
    """(agent razonamiento/reporting #1, CRITICAL) /stop with a running
    turn AND a sleeping queue at once blew up with TypeError — the
    emergency command must not fail right there."""

    def test_corrida_y_cola_juntas_arman_un_solo_listado(self):
        zt = support.fresh(self)
        from zcode_bot import reporting
        from zcode_bot import workers as zw
        from zcode_bot.core import _workers
        # one worker running (items)…
        _workers().register(support.T1,
                            {"t0": 1, "pid": 424242, "titulo": "testing",
                             "prompt": "x"})
        # …and a sleeping queue from ANOTHER session (extras)
        zw._queue_put(support.T2, {"sid": support.T2, "title": "CV",
                                   "ws": support.PROJ_CV}, "pendiente")
        r = reporting.stop_list_report()
        texto, kb = r if isinstance(r, tuple) else (r, None)
        self.assertIn("Phone runs", texto)
        self.assertIn("Queued messages", texto)       # both, no blow-up
        botones = [b["text"] for f in (kb or {}).get("inline_keyboard", [])
                   for b in f]
        self.assertTrue(any("clear queue" in b for b in botones))


class TestCajasConEscapables(unittest.TestCase):
    """(inflation family) boxes and viewer with code inside (& < >) —
    they arrive WHOLE, not cut by send's truncation."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_mostrar_parte_por_escape(self):
        from zcode_bot import razonamiento as rz
        sid, t0 = support.T1, 1760000000
        caja = "&" * 3000                 # esc() inflates it to 15000
        rz.guardar(sid, t0, "testing", [caja], ["CoT 1.1"], ph=555,
                   phs=[555])
        r = rz.mostrar(sid, t0, 1)
        if isinstance(r, tuple):
            r = r[0]
        for parte in r.split("<pre>")[1:]:
            # each visible part fits WHOLE in one message
            self.assertLessEqual(len(parte.split("</pre>")[0]),
                                 lim.CH_RESPUESTA)
        self.assertTrue(r.count("&amp;") >= 100)  # real content, not short

    def test_tool_caja_recorta_por_escape(self):
        from zcode_bot.messages import _tool_caja
        caja = _tool_caja("Bash", {"command": "&" * 9000,
                                   "output": "ok"})
        self.assertIn("&amp;", caja)
        self.assertLessEqual(len(caja), lim.CH_CAJA + 800 + 600)

    def test_recorte_html_prefijo_maximo(self):
        from zcode_bot.utils.formatting import recorte_html
        self.assertEqual(recorte_html("hola", 100), "hola")
        t = recorte_html("&" * 1000, 500)
        self.assertLessEqual(len(esc(t)), 500)     # the escape FITS
        self.assertEqual(t, "&" * len(t))          # clean prefix


class TestCacheAtomico(unittest.TestCase):
    def test_guardar_no_deja_archivo_truncado_a_la_vista(self):
        zt = support.fresh(self)
        from zcode_bot import razonamiento as rz
        sid, t0 = support.T1, 1760000000
        rz.guardar(sid, t0, "t", ["caja"], ["CoT 1.1"], vivo=True)
        # a reader mid-write NEVER sees truncation: the final path
        # always holds complete JSON (tmp + replace)
        with open(rz.cache_path(sid, t0)) as f:
            d = json.load(f)
        self.assertTrue(d["vivo"])
        self.assertEqual(d["cajas"], ["caja"])
        rz.cerrar_cache(sid, t0)
        with open(rz.cache_path(sid, t0)) as f:
            self.assertTrue(json.load(f)["finalizado"])
        sobras = [p for p in os.listdir(zt.config.STATE_DIR)
                  if ".tmp." in p]
        self.assertEqual(sobras, [])


class TestSelloDeLineas(unittest.TestCase):
    """F50b — Damian: «every file you show, diff or plain, with its line
    count and green and red circles — all of them»."""

    def test_sello_con_diff_y_comun(self):
        from zcode_bot.utils.formatting import sello_lineas
        self.assertEqual(sello_lineas(3, 1, 40), "🟢 +3 · 🔴 −1 · 40 lines")
        self.assertEqual(sello_lineas(3, 0, 40), "🟢 +3 · 40 lines")
        self.assertEqual(sello_lineas(0, 0, 40), "40 lines")     # plain
        self.assertEqual(sello_lineas("-", "-"), "—")           # binary

    def test_caja_de_write_lleva_el_sello(self):
        from zcode_bot.messages import _tool_caja
        caja = _tool_caja("Write", {"content": "uno\ndos\ntres"})
        self.assertIn("🟢 +3 · 3 lines", caja)
        self.assertTrue(caja.count("🟢") >= 3)   # and the green lines

    def test_caja_de_edit_lleva_el_diffstat(self):
        from zcode_bot.messages import _tool_caja
        caja = _tool_caja("Edit", {"old_string": "a\nb",
                                   "new_string": "a\nc\nd"})
        self.assertIn("🟢 +2", caja)
        self.assertIn("🔴 −1", caja)


class TestPollerBatchSobrevive(unittest.IsolatedAsyncioTestCase):
    """(G) An exception in message k confirmed the offset of the WHOLE
    batch: the queue (k+1..n) was lost with no reprocessing. Now the
    poison message is logged and the rest of the batch runs."""

    async def test_el_venenoso_no_arrastra_la_cola_del_batch(self):
        from test_poller import FakeDeps, _msg

        class _Deps(FakeDeps):
            def process(self, cfg, msg):
                if msg.get("text") == "/b":    # only /b is poisonous
                    raise RuntimeError("boom")
                super().process(cfg, msg)

        d = _Deps(batches=[[_msg(1, "/a"), _msg(2, "/b"), _msg(3, "/c")]])
        await d.run()
        self.assertEqual(d.procesados, ["/a", "/c"])  # b blew up, c ran
        self.assertEqual(d.saved, [101])              # and the batch advanced
        self.assertTrue(any("process crashed" in e for e in d.errores))


if __name__ == "__main__":
    unittest.main()
