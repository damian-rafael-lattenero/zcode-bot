# -*- coding: utf-8 -*-
"""F50 — fixes de la auditoría profunda del 20/10 («leé todo, buscá
errores por todos lados y arreglalos»).

Cuatro agentes + herramientas automáticas (pyflakes, AST de imports)
leyeron todo el paquete. Los bugs arreglados acá:

1. truncar_html (utils/formatting): send/_live_send/_live_edit truncaban
   el HTML YA construido en 3900 chars — el corte caía con un <pre>
   abierto (su cierre vive al final) y Telegram rechazaba el mensaje
   ENTERO con 400 «Unclosed tag»: perdido + outbox reintentándolo para
   siempre. La inflación venía de esc() (&→5x, <>→4x) sobre los caps
   medidos en crudo.
2. _partes_por_escape (reporting): _send_long_reply partía en 3900
   CRUDOS y escapaba después — una parte inflaba a ~5x y send la
   truncaba: respuesta cortada en silencio con el «(2/3)» mintiendo.
3. BotSession.mode_set (core): prompt_worker lo escribía tras setMode
   pero from_dict LO DESCARTABA — la optimización skip_mode estaba
   muerta y cada turno frío pagaba session/setMode de nuevo.
4. save_config atómico: crash a mitad de json.dump → archivo truncado →
   load_config caía a DEFAULTS (token vacío) → bot sordo sin aviso.
5. Respuesta «0» a una pregunta: índice negativo seleccionaba la
   ÚLTIMA opción de prepo.
6. /cotb con n=0: phs[-1] apuntaba a la última razonada.
7. read_quota_cache: max() sobre limits vacío reventaba /quota.
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
        # el caso del /diff de HTML o una caja CoT con muchos escapes
        texto = "<pre>" + "x" * 8000 + "</pre>"
        t = truncar_html(texto, 3900)
        self.assertLessEqual(len(t), 3900 + len("</pre>"))
        self.assertTrue(t.startswith("<pre>"))
        self.assertTrue(t.endswith("</pre>"))  # Telegram lo acepta entero

    def test_entidad_partida_por_el_corte(self):
        texto = "a" * 3980 + " &amp; y sigue"
        t = truncar_html(texto, 3900)
        self.assertFalse(t.endswith("&am"))    # la entidad rota, fuera
        self.assertNotIn("&am;", t)

    def test_tag_partido_por_el_corte(self):
        texto = "a" * 3899 + "<pre>resto"
        t = truncar_html(texto, 3900)
        self.assertFalse(t.rstrip().endswith("<"))  # el <pr suelto, fuera

    def test_anidados_se_cierran_en_orden(self):
        texto = "<b>negrita <pre>%s</pre></b>" % ("y" * 9000)
        t = truncar_html(texto, 40)
        self.assertTrue(t.endswith("</pre></b>"))


class TestSendTruncaSeguro(unittest.TestCase):
    """send() usa truncar_html: lo que llega a Telegram siempre parsea."""

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
        texto = "<pre>%s</pre>" % ("&<>" * 5000)   # esc() infla 5x
        zc_http.send(self.cfg, texto)
        t = self.params[0]["text"]
        self.assertLessEqual(len(t), 4096)
        self.assertTrue(t.endswith("</pre>"))
        # sin entidades partidas
        self.assertNotIn("&am;", t.replace("&amp;", ""))
        self.assertNotIn("&l", t.replace("&lt;", ""))


class TestPartesPorEscape(unittest.TestCase):
    def test_texto_inflado_parte_completo(self):
        from zcode_bot.reporting import _partes_por_escape
        cap = lim.CH_RESPUESTA - 300
        texto = "&" * 20000           # esc() lo infla 5x
        partes = _partes_por_escape(texto, cap)
        self.assertGreater(len(partes), 1)
        for p in partes:
            self.assertLessEqual(len(esc(p)), cap)
        self.assertEqual("".join(partes), texto)   # nada se pierde

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
        # sin mode_set no aparece (shape histórico preservado)
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
            # el "0" viaja COMO TEXTO (el teclado numera desde 1); con el
            # bug del índice negativo respondía "no" (la ÚLTIMA opción)
            self.assertIn("Respondido: 0", r)
            self.assertNotIn("Respondido: no", r)
        finally:
            _questions().answer("req-1", None)   # limpia la pregunta


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
        # n=0: fuera de rango — se muestra como aviso sobre el ph GLOBAL
        r = zt.handle_command(
            dict(zt.DEFAULTS, token="T", chat_id=1),
            "/cotb %s %d 0" % (sid, t0))
        self.assertEqual(edits, [555])       # el global, NO el 222
        self.assertNotEqual(edits, [222])


class TestColaHonesta(unittest.TestCase):
    """(H) El flock vencido no se traga más: el dueño se entera y /qgo
    no duplica lanzando sin haber sacado el ítem."""

    def setUp(self):
        self.zt = support.fresh(self)

    def _cola_trabada(self, mod):
        """Un _store cuyo flock siempre vence — SOLO en el módulo dado
        (el registry usa update_cross_process para otras cosas)."""

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
        self._cola_trabada(zw)   # _queue_put resuelve _store en workers
        with mock.patch.object(ht, "running_turns", lambda: {sid}):
            r = ht.handle_free_text(
                dict(self.zt.DEFAULTS, token="T", chat_id=1), "hacé algo")
        self.assertIn("NO pude encolar", r)     # el aviso honesto

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
        self.assertIn("trabada", r)
        self.assertEqual(spawns, [])            # nada lanzado: sin duplicar


class TestQuotaLimitsVacios(unittest.TestCase):
    def test_limits_vacio_no_revienta(self):
        zt = support.fresh(self)
        from zcode_bot import reporting
        # leveldb de mentira en el fixture (el path real es absoluto:
        # expanduser condicional hacia acá)
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
                return ldir   # el leveldb de mentira del fixture
            return real(p)
        with mock.patch("os.path.expanduser", fake_exp):
            r = reporting.read_quota_cache()
        self.assertIsNotNone(r)   # el sano ganó sin ValueError
        self.assertEqual(r["windows"][0]["hours"], 5)


class TestCarreraDeRegister(unittest.TestCase):
    """(agente workers/live_view #1) El retorno de register() es la
    señal anti-duplicación: perder la carrera contra otro spawn debe
    ENCOLAR, no correr un segundo turno sobre la misma sesión."""

    def test_perder_la_carrera_encola_y_no_arranca(self):
        zt = support.fresh(self)
        from zcode_bot import workers as zw

        class _RegistryCiego:
            """get() no ve al otro worker la PRIMERA vez (la carrera) —
            register() devuelve False porque YA está registrado."""
            def __init__(self, real):
                self.real = real
                self.veces = 0

            def get(self, sid):
                self.veces += 1
                return None if self.veces == 1 else self.real.get(sid)

            def __getattr__(self, n):
                return getattr(self.real, n)

        reg = zt._workers()
        # un worker DE OTRO pid ya registró la sesión
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
    """(agente #2) El ítem cuyo spawn falló vuelve AL FRENTE — el put
    al final invertía el orden de los mensajes del usuario."""

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
                         ["A", "B", "C"])   # A sigue primero


class TestStopCombinado(unittest.TestCase):
    """(agente razonamiento/reporting #1, CRÍTICO) /stop con corrida Y
    cola dormida a la vez revienta con TypeError — el comando de
    emergencia no puede fallar justo ahí."""

    def test_corrida_y_cola_juntas_arman_un_solo_listado(self):
        zt = support.fresh(self)
        from zcode_bot import reporting
        from zcode_bot import workers as zw
        from zcode_bot.core import _workers
        # un worker corriendo (items)…
        _workers().register(support.T1,
                            {"t0": 1, "pid": 424242, "titulo": "testing",
                             "prompt": "x"})
        # …y una cola dormida de OTRA sesión (extras)
        zw._queue_put(support.T2, {"sid": support.T2, "title": "CV",
                                   "ws": support.PROJ_CV}, "pendiente")
        r = reporting.stop_list_report()
        texto, kb = r if isinstance(r, tuple) else (r, None)
        self.assertIn("Corridas del celular", texto)
        self.assertIn("Mensajes en cola", texto)      # ambos, sin reventar
        botones = [b["text"] for f in (kb or {}).get("inline_keyboard", [])
                   for b in f]
        self.assertTrue(any("limpiar cola" in b for b in botones))


class TestCajasConEscapables(unittest.TestCase):
    """(familia inflación) cajas y visor con código adentro (& < >) —
    llegan ENTERAS, no cortadas por el truncado del send."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_mostrar_parte_por_escape(self):
        from zcode_bot import razonamiento as rz
        sid, t0 = support.T1, 1760000000
        caja = "&" * 3000                 # esc() lo infla a 15000
        rz.guardar(sid, t0, "testing", [caja], ["CoT 1.1"], ph=555,
                   phs=[555])
        r = rz.mostrar(sid, t0, 1)
        if isinstance(r, tuple):
            r = r[0]
        for parte in r.split("<pre>")[1:]:
            # cada parte visible entra COMPLETA en un mensaje
            self.assertLessEqual(len(parte.split("</pre>")[0]),
                                 lim.CH_RESPUESTA)
        self.assertTrue(r.count("&amp;") >= 100)  # contenido real, no corto

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
        self.assertLessEqual(len(esc(t)), 500)     # el escape ENTRA
        self.assertEqual(t, "&" * len(t))          # prefijo limpio


class TestCacheAtomico(unittest.TestCase):
    def test_guardar_no_deja_archivo_truncado_a_la_vista(self):
        zt = support.fresh(self)
        from zcode_bot import razonamiento as rz
        sid, t0 = support.T1, 1760000000
        rz.guardar(sid, t0, "t", ["caja"], ["CoT 1.1"], vivo=True)
        # un lector a mitad de escritura NUNCA ve truncado: el path
        # final siempre tiene JSON completo (tmp + replace)
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
    """F50b — Damian: «todo archivo que muestres, sea de diff o sea
    común, con su número de líneas y redondel verde y rojo — todos»."""

    def test_sello_con_diff_y_comun(self):
        from zcode_bot.utils.formatting import sello_lineas
        self.assertEqual(sello_lineas(3, 1, 40), "🟢 +3 · 🔴 −1 · 40 líneas")
        self.assertEqual(sello_lineas(3, 0, 40), "🟢 +3 · 40 líneas")
        self.assertEqual(sello_lineas(0, 0, 40), "40 líneas")   # común
        self.assertEqual(sello_lineas("-", "-"), "—")           # binario

    def test_caja_de_write_lleva_el_sello(self):
        from zcode_bot.messages import _tool_caja
        caja = _tool_caja("Write", {"content": "uno\ndos\ntres"})
        self.assertIn("🟢 +3 · 3 líneas", caja)
        self.assertTrue(caja.count("🟢") >= 3)   # y las líneas verdes

    def test_caja_de_edit_lleva_el_diffstat(self):
        from zcode_bot.messages import _tool_caja
        caja = _tool_caja("Edit", {"old_string": "a\nb",
                                   "new_string": "a\nc\nd"})
        self.assertIn("🟢 +2", caja)
        self.assertIn("🔴 −1", caja)


class TestPollerBatchSobrevive(unittest.IsolatedAsyncioTestCase):
    """(G) Una excepción en el mensaje k confirmaba el offset de TODO el
    batch: la cola (k+1..n) se perdía sin reprocesar. Ahora el venenoso
    se loguea y el resto del batch corre."""

    async def test_el_venenoso_no_arrastra_la_cola_del_batch(self):
        from test_poller import FakeDeps, _msg

        class _Deps(FakeDeps):
            def process(self, cfg, msg):
                if msg.get("text") == "/b":    # solo /b es venenoso
                    raise RuntimeError("boom")
                super().process(cfg, msg)

        d = _Deps(batches=[[_msg(1, "/a"), _msg(2, "/b"), _msg(3, "/c")]])
        await d.run()
        self.assertEqual(d.procesados, ["/a", "/c"])  # b explotó, c corrió
        self.assertEqual(d.saved, [101])              # y el batch avanzó
        self.assertTrue(any("process reventó" in e for e in d.errores))


if __name__ == "__main__":
    unittest.main()
