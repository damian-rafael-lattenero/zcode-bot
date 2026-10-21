"""F21 — la promesa se cumple: ningún turno muere en silencio.

Dos caminos cubiertos:
1. (HISTÓRICO, R0b lo volvió imposible) Worker ABANDONA a los 40 min:
   hoy el worker NO abandona — la espera del fin no tiene tope (aviso
   "⏱️ lleva N min" cada 30) y el único corte es /stop. El abandono
   MATABA el runtime ("Turn was cancelled" al salir el worker, 20/10
   14:55) — ver TestTurnEndPollSinTope.
2. Worker MUERE abruptamente (kill -9/OOM, sin finally): la entrada queda
   con pid muerto → supervisor (_orphans_check) detecta → entrega (o
   honesto "murió").

F22bis: cajas permanentes de tools (comando completo + diffs), pedido de
Damian: "ver además del CoT las tools llamadas, los commands completos y
los archivos modificados con el diff".
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))
from zcode_bot import config as zc_config  # noqa: E402


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _FakeProc:
    def __init__(self):
        self.returncode = None  # vivo

    def poll(self):
        return self.returncode


class TestTurnEndPollSinTope(unittest.TestCase):
    """R0b — el worker NO abandona turnos largos: _turn_end_poll con
    timeout=None sigue esperando más allá de los viejos 40 min (cuando el
    abandono mataba el runtime: turn.failed "Turn was cancelled" el mismo
    segundo que el worker salía — CLI log 20/10 14:55) y avisa cada 30 min
    de turno corrido mientras espera."""

    def test_sigue_pasado_los_40_y_avisa_cada_30(self):
        from zcode_bot import live_view as lv

        class _Reloj:  # 15 min por vuelta: cruza 30 y 60 antes del fin
            now = 1000.0

            def time(self):
                type(self).now += 900.0
                return self.now

            def sleep(self, s):  # pacing del poll: no avanza el reloj
                pass

        class _Conn:
            n = 0

            def request(self, method, params=None, timeout=None):
                type(self).n += 1
                evs = [{"seq": self.n, "type": "turn.started",
                        "payload": {"inputId": "in1",
                                    "executionStartedAt": 1_001_000}}]
                if self.n >= 5:  # el turno termina al minuto ~75
                    evs.append({"seq": self.n + 1, "type": "turn.completed",
                                "payload": {"inputId": "in1",
                                            "resultType": "success"}})
                return {"result": {"events": evs}}

        reloj = _Reloj()
        support.parchea(self, lv, "time", reloj)
        avisos = []
        fin = lv._turn_end_poll("sid", t0=950.0, timeout=None, t_send=1000.0,
                                conn=_Conn(),
                                aviso=lambda m: avisos.append(m))
        # con el tope viejo (1800+600) habría devuelto None a los ~30-40 min
        self.assertEqual(fin["event"], "turn.completed")
        self.assertEqual(avisos, [30, 60])


class _FakeClient:
    def __init__(self):
        self.p = _FakeProc()
        self.cerrado = False

    def close(self):
        self.cerrado = True


class TestTurnoActivo(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_register_y_clear(self):
        zt = self.zt
        zt._active_turn_register("sess_X", "testing", 123.0,
                                 ws="/w", last_id="m1")
        ent = zt._store("bot-active.json").read()["sess_X"]
        self.assertEqual(ent["titulo"], "testing")
        self.assertEqual(ent["last_id"], "m1")
        self.assertEqual(ent["pid"], os.getpid())
        zt._active_turn_clear("sess_X")
        self.assertEqual(zt._store("bot-active.json").read(), {})

    async def test_register_sin_sid_es_noop(self):
        self.zt._active_turn_register(None, "t", 1.0)
        self.assertIsNone(self.zt._store("bot-active.json").read())


class TestOrphansCheck(unittest.IsolatedAsyncioTestCase):
    """El supervisor: pid muerto + fin → entrega; sin fin → honesto."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1
        self.sends = []
        # F4g: _orphans_check vive en workers (uhttp.*, bindings propios)
        from zcode_bot import workers as zc_workers
        from zcode_bot.utils import http as zc_http
        ps = mock.patch.object(
            zc_http, "send",
            lambda cfg, text, loud=None, markup=None:
                (self.sends.append(text), True)[1])
        ps.start()
        self.addCleanup(ps.stop)
        self.respuestas = []

        def fake_long(cfg, titulo, dur, texto, max_partes=3):
            self.respuestas.append((titulo, texto))
        pl = mock.patch.object(zc_workers, "_send_long_reply", fake_long)
        pl.start()
        self.addCleanup(pl.stop)

    def _registrar_muerto(self, sid="sess_X", t0=None, ws=None):
        t0 = t0 or (time.time() - 60)
        self.zt._active_turn_register(sid, "testing", t0, ws=ws)
        # pid muerto de verdad (proceso ya finalizado)
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()

        def fn(q):
            q[sid]["pid"] = p.pid
            return q
        self.zt._store("bot-active.json").update_cross_process(fn, {})
        return t0

    def _log_fin(self, sid, event, t0):
        log_hoy = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        # el fin llega DESPUÉS del registro del turno (since_epoch=t0)
        with open(log_hoy, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(t0 + 5),
                                "event": event, "sessionId": sid,
                                "turnId": "turn_X"}) + "\n")

    async def test_muerto_sin_fin_avisa_honesto(self):
        zt = self.zt
        self._registrar_muerto()
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("murió con su worker" in s for s in self.sends))
        self.assertIsNone((zt._store("bot-active.json").read() or {})
                          .get("sess_X"))

    async def test_muerto_con_completado_entrega_texto(self):
        zt = self.zt
        t0 = time.time() - 120
        self._registrar_muerto(t0=t0)
        self._log_fin("sess_X", "turn.completed", t0)
        class FakeC:
            def request(self, method, params=None, timeout=None):
                if method == "session/resume":
                    return {"result": {}}
                if method == "session/messages":
                    return {"result": {"messages": [
                        {"info": {"role": "assistant", "id": "m2"},
                         "parts": [{"type": "text",
                                    "text": "respuesta del huérfano"}]}]}}
                return {}

        def fake_make(cwd=None, **kw):
            return FakeC()
        from zcode_bot import workers as zc_workers
        support.parchea(self, zc_workers, "zc_proto",
                        type("M", (), {"make": staticmethod(fake_make)}))
        zt._orphans_check(_cfg(zt))
        self.assertEqual(len(self.respuestas), 1)
        self.assertEqual(self.respuestas[0][1], "respuesta del huérfano")

    async def test_muerto_con_failed_avisa_fallo(self):
        zt = self.zt
        t0 = time.time() - 120
        self._registrar_muerto(t0=t0)
        self._log_fin("sess_X", "turn.failed", t0)
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("falló" in s for s in self.sends))
        self.assertFalse(any("TERMINÓ" in s for s in self.sends))

    async def test_vivo_no_hace_nada(self):
        zt = self.zt
        zt._active_turn_register("sess_X", "t", time.time() - 10)
        zt._orphans_check(_cfg(zt))
        self.assertEqual(self.sends, [])
        self.assertIsNotNone((zt._store("bot-active.json").read() or {})
                             .get("sess_X"))

    async def test_purge_por_edad(self):
        zt = self.zt
        self._registrar_muerto(t0=time.time() - 7201)
        # pid muerto PERO entrada de >2h: purge silencioso, sin aviso
        zt._orphans_check(_cfg(zt))
        self.assertEqual(self.sends, [])
        self.assertIsNone((zt._store("bot-active.json").read() or {})
                          .get("sess_X"))

    # ---- F47c: el /stop que remata al worker se cuenta como /stop, no fallo

    async def test_failed_tras_stop_dice_frenada_no_fallo(self):
        # el /stop vence la gracia → kill -9 → el worker no pasa por su
        # finally → pid muerto + turn.failed: antes «❌ falló (el worker
        # había muerto)» para algo que pidió el usuario (20/10 13:03)
        from zcode_bot import workers as zc_workers
        zt = self.zt
        t0 = time.time() - 120
        self._registrar_muerto(t0=t0)
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        self._log_fin("sess_X", "turn.failed", t0)
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("frenada por tu /stop" in s for s in self.sends))
        self.assertFalse(any("falló" in s or "había muerto" in s
                             for s in self.sends))
        self.assertIsNone((zt._store("bot-active.json").read() or {})
                          .get("sess_X"))

    async def test_sin_fin_tras_stop_dice_frenada_no_oom(self):
        from zcode_bot import workers as zc_workers
        zt = self.zt
        self._registrar_muerto()
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("frenada por tu /stop" in s for s in self.sends))
        self.assertFalse(any("kill/OOM" in s for s in self.sends))

    async def test_completado_tras_stop_entrega_igual(self):
        # el stop llegó tarde y el turno terminó: la respuesta se entrega
        from zcode_bot import workers as zc_workers
        zt = self.zt
        t0 = time.time() - 120
        self._registrar_muerto(t0=t0)
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        self._log_fin("sess_X", "turn.completed", t0)
        support.parchea(self, zc_workers, "_orphan_fetch_text",
                        lambda ent, sid: "llegó igual")
        zt._orphans_check(_cfg(zt))
        self.assertEqual([r[1] for r in self.respuestas], ["llegó igual"])
        self.assertFalse(any("frenada" in s for s in self.sends))

    async def test_mark_sin_entrada_no_la_crea(self):
        from zcode_bot import workers as zc_workers
        zc_workers._active_turn_mark("sess_nada", stop=1.0)
        self.assertFalse((self.zt._store("bot-active.json").read() or {})
                         .get("sess_nada"))

    # ---- F47d: el cache CoT del turno muerto se cierra

    async def test_cache_cot_queda_cerrado_y_entero(self):
        # el último guardar del worker fue vivo=True; muerto el worker nadie
        # lo cerraba y el JSON decía «vivo» para siempre
        from zcode_bot import razonamiento as rz
        zt = self.zt
        t0 = time.time() - 120
        self._registrar_muerto(t0=t0)
        rz.guardar("sess_X", t0, "testing", ["caja"], ["CoT 1.1"],
                   vivo=True, ph=5, phs=[5])
        zt._orphans_check(_cfg(zt))
        with open(rz.cache_path("sess_X", t0)) as f:
            d = json.load(f)
        self.assertNotIn("vivo", d)
        self.assertTrue(d["finalizado"])
        self.assertIn("kill/OOM", d["error"])
        # lo que el tap necesita sigue intacto
        self.assertEqual((d["cajas"], d["rotulos"], d["ph"], d["phs"]),
                         (["caja"], ["CoT 1.1"], 5, [5]))

    async def test_cache_cot_tras_stop_dice_frenado(self):
        from zcode_bot import razonamiento as rz
        from zcode_bot import workers as zc_workers
        zt = self.zt
        t0 = time.time() - 120
        self._registrar_muerto(t0=t0)
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        rz.guardar("sess_X", t0, "testing", ["caja"], ["CoT 1.1"], vivo=True)
        zt._orphans_check(_cfg(zt))
        with open(rz.cache_path("sess_X", t0)) as f:
            d = json.load(f)
        self.assertEqual(d["error"], "frenado por /stop")
        self.assertNotIn("vivo", d)

    async def test_sin_cache_no_lo_inventa(self):
        from zcode_bot import razonamiento as rz
        t0 = time.time() - 120
        self._registrar_muerto(t0=t0)
        self.zt._orphans_check(_cfg(self.zt))
        self.assertFalse(os.path.exists(rz.cache_path("sess_X", t0)))


class TestCerrarCache(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_sin_archivo_devuelve_false(self):
        from zcode_bot import razonamiento as rz
        self.assertFalse(rz.cerrar_cache("sess_X", 1))
        self.assertFalse(os.path.exists(rz.cache_path("sess_X", 1)))

    def test_cierra_sin_tocar_el_resto(self):
        from zcode_bot import razonamiento as rz
        rz.guardar("sess_X", 1, "t", ["a", "b"], ["CoT 1.1", "CoT 1.2"],
                   vivo=True, ph=9, phs=[9, 9])
        self.assertTrue(rz.cerrar_cache("sess_X", 1))
        with open(rz.cache_path("sess_X", 1)) as f:
            d = json.load(f)
        self.assertEqual(d, {"sid": "sess_X", "titulo": "t", "t0": 1,
                             "cajas": ["a", "b"],
                             "rotulos": ["CoT 1.1", "CoT 1.2"],
                             "ph": 9, "phs": [9, 9], "finalizado": True})

    def test_json_roto_devuelve_false(self):
        from zcode_bot import razonamiento as rz
        os.makedirs(zc_config.STATE_DIR, exist_ok=True)
        with open(rz.cache_path("sess_X", 1), "w") as f:
            f.write("{roto")
        self.assertFalse(rz.cerrar_cache("sess_X", 1))


class TestRuntimeMuerto(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_poll_none_es_vivo(self):
        zt = self.zt
        self.assertFalse(zt._runtime_muerto(_FakeClient()))

    async def test_poll_numero_es_muerto(self):
        zt = self.zt
        c = _FakeClient()
        c.p.returncode = -9
        self.assertTrue(zt._runtime_muerto(c))

    async def test_sin_proc_no_afirma_muerte(self):
        # sin .p no hay evidencia: conservador = vivo (no disparar falsos
        # "murió" sobre clientes de otra forma)
        self.assertFalse(self.zt._runtime_muerto(object()))


class TestToolCajas(unittest.IsolatedAsyncioTestCase):
    """F22bis — el pedido: tools completas, commands completos, diffs."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_bash_comando_completo(self):
        caja = self.zt._tool_caja("Bash", {"command": "git status && git log"})
        self.assertIn("git status &amp;&amp; git log", caja)
        self.assertIn("🔧 <b>Bash</b>", caja)

    async def test_edit_diff_real(self):
        caja = self.zt._tool_caja(
            "Edit", {"file_path": "/x/y.py",
                     "old_string": "a = 1\nb = 2",
                     "new_string": "a = 42\nb = 2"})
        # F7c+: diff a lo git diff — 🟢 agrega / 🔴 borra, uno por línea,
        # con diffstat de totales en la primera línea (● 🟢 +1 🔴 −1)
        self.assertIn("● 🟢 +1  🔴 −1", caja)
        self.assertIn("🔴 -a = 1", caja)
        self.assertIn("🟢 +a = 42", caja)
        self.assertIn("y.py", caja)

    async def test_multiedit_un_diff_por_edit(self):
        # antes caía al fallback de JSON crudo («lo vi todo junto»)
        caja = self.zt._tool_caja(
            "MultiEdit", {"file_path": "/x/y.py", "edits": [
                {"old_string": "a = 1", "new_string": "a = 42"},
                {"old_string": "b = 2", "new_string": "b = 7"}]})
        self.assertIn("edit 1", caja)
        self.assertIn("edit 2", caja)
        self.assertIn("● 🟢 +1  🔴 −1", caja)  # diffstat por edit
        self.assertIn("🟢 +a = 42", caja)
        self.assertIn("🔴 -b = 2", caja)
        self.assertNotIn('"edits"', caja)  # no es JSON crudo

    async def test_write_cuenta_lineas(self):
        caja = self.zt._tool_caja(
            "Write", {"file_path": "/x/nuevo.py",
                      "content": "linea1\nlinea2\nlinea3"})
        self.assertIn("🟢 linea1", caja)
        self.assertIn("🟢 +3 · 3 líneas", caja)  # F50b: el sello

    async def test_filtro_de_cajas_por_preset(self):
        # 'accion' (default): Read solo índice, Bash/Edit/Agent/MCP con caja
        on = self.zt._tool_caja_on
        self.assertFalse(on("Read", {}))
        self.assertFalse(on("TodoWrite", {}))
        self.assertFalse(on("Grep", {}))
        self.assertTrue(on("Bash", {}))
        self.assertTrue(on("Edit", {}))
        self.assertTrue(on("Agent", {}))
        self.assertTrue(on("mcp__serper__search_web", {}))
        # 'todas': statu quo F7
        self.assertTrue(on("Read", {"tools_caja_filtro": "todas"}))
        # 'minimo': solo bash + ediciones
        self.assertFalse(on("Agent", {"tools_caja_filtro": "minimo"}))
        self.assertFalse(on("mcp__serper__search_web",
                            {"tools_caja_filtro": "minimo"}))
        self.assertTrue(on("Write", {"tools_caja_filtro": "minimo"}))

    async def test_cap_3400(self):
        caja = self.zt._tool_caja("Bash", {"command": "x" * 20000})
        self.assertLess(len(caja), support.CH_CAJA + 800)  # formato + header

    async def test_output_recortada(self):
        caja = self.zt._tool_caja(
            "Bash", {"command": "ls", "output": "a\nb\nc"})
        self.assertIn("└", caja)
        self.assertIn("a\nb\nc", caja)

    async def test_tools_of_messages_shape(self):
        msgs = [{"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "tool": "Bash", "callID": "call_1",
             "state": {"input": {"command": ["echo", "hola"]},
                       "status": "completed"}},
            {"type": "text", "text": "x"}]}]
        tools = self.zt._tools_of_messages(msgs)
        self.assertEqual(tools, [("Bash", {"command": ["echo", "hola"]})])
        # F7c: la variante con callID empareja con los toolCallId del stream
        self.assertEqual(self.zt._tools_con_call_id(msgs),
                         [("call_1", "Bash", {"command": ["echo", "hola"]},
                           "completed")])
        # compat: _steps_of_messages sigue devolviendo (nombre, desc)
        self.assertEqual(self.zt._steps_of_messages(msgs),
                         [("Bash", "echo hola")])

    async def test_cajas_stream_apagable(self):
        # F4d: _cajas_paso_nuevas vive en live_view y usa el transporte por
        # atributo (uhttp.send) — el seam de parcheo es el módulo dueño
        from unittest import mock
        from zcode_bot.utils import http as zc_http
        envia = []
        with mock.patch.object(zc_http, "send",
                               lambda cfg, text, loud=None, markup=None:
                               (envia.append(text), True)[1]):
            self.zt._cajas_paso_nuevas(_cfg(self.zt), "t", [0],
                                       [(None, "Bash", {"command": "ls"},
                                         None)])
            self.assertEqual(len(envia), 1)
            self.zt._cajas_paso_nuevas(_cfg(self.zt, tools_stream=False),
                                       "t", [0],
                                       [(None, "Bash", {"command": "ls"},
                                         None)])
            self.assertEqual(len(envia), 1)  # no cambió
            # F7c: la tool con caja viva propia (nacida 🔵 del stream) no
            # se re-manda como caja completa
            self.zt._cajas_paso_nuevas(_cfg(self.zt), "t", [0],
                                       [("call_9", "Edit",
                                         {"file_path": "a"}, "completed")],
                                       boxed={"call_9"})
            self.assertEqual(len(envia), 1)  # sigue sin cambiar


if __name__ == "__main__":
    unittest.main()
