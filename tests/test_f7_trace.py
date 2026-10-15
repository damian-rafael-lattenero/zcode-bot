"""F7: trace de turnos — paso a paso + CoT completo en Telegram.

Congela: extractores de steps/reasoning del historial, cuerpo multipart de
sendDocument, build_trace, _turn_trace (persistencia + flag) y presencia de
/cot en registro/menú/help.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import support  # inserta src/ en sys.path (determinismo de colección)
from zcode_bot import config as zc_config  # noqa: E402


def _msg(role, parts):
    return {"info": {"role": role, "id": "m%d" % int(abs(hash(str(parts))) % 10**8)},
            "parts": parts}


MSGS = [
    _msg("user", [{"type": "text", "text": "arreglalo"}]),
    _msg("assistant", [
        {"type": "step-start"},
        {"type": "reasoning", "text": "Pienso verificar el archivo."},
        {"type": "tool", "tool": "Read", "state": {"input": {"file_path": "/a/b.py"}}},
    ]),
    _msg("assistant", [
        {"type": "reasoning", "text": "Ahora corro los tests."},
        {"type": "tool", "tool": "Bash",
         "state": {"input": {"command": ["python3", "-m", "unittest", "-q"]}}},
        {"type": "tool", "tool": "Edit",
         "state": {"input": {"file_path": "/a/b.py"}}},
    ]),
    _msg("assistant", [
        {"type": "text", "text": "Listo, arreglado."},
    ]),
]


class TestCotChunking(unittest.IsolatedAsyncioTestCase):
    """Troceador del CoT incremental: cajas permanentes de 3000 mientras
    la cola viva retiene lo último (límite 4096 de Telegram)."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_bajo_el_umbral_no_manda_nada(self):
        out, flushed = self.zt._cot_chunks_to_send("x" * 100, 0)
        self.assertEqual(out, [])
        self.assertEqual(flushed, 0)

    async def test_excedente_se_troza_en_3000(self):
        buf = "".join(str(i % 10) for i in range(10000))
        out, flushed = self.zt._cot_chunks_to_send(buf, 0)
        self.assertEqual([len(t) for t in out], [3000, 3000, 3000])  # resta 1000 ≤ cola
        self.assertEqual(flushed, 9000)
        self.assertEqual(out[0], buf[:3000])
        self.assertEqual(out[2], buf[6000:9000])

    async def test_sin_tope_se_mandan_todos(self):
        """2026-10-13: Damian pidió CoT SIN tope — 200K chars = 66 cajas
        de 3000 (la cola viva retiene los últimos ~2000)."""
        buf = "y" * 200000
        out, flushed = self.zt._cot_chunks_to_send(buf, 0)
        self.assertEqual(len(out), 66)
        self.assertEqual(flushed, 66 * 3000)
        self.assertEqual("".join(out), buf[:flushed])

    async def test_flush_final_se_lleva_todo(self):
        buf = "z" * 5000
        out, flushed = self.zt._cot_chunks_to_send(buf, 0, tail_keep=0)
        self.assertEqual(flushed, 5000)            # sin cola: todo el resto
        self.assertEqual("".join(out), buf)


class TestExtractores(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_steps_de_tools_con_command_y_file(self):
        pasos = self.zt._steps_of_messages(MSGS)
        self.assertEqual(pasos, [
            ("Read", "/a/b.py"),
            ("Bash", "python3 -m unittest -q"),
            ("Edit", "/a/b.py"),
        ])

    async def test_steps_tolerante_sin_input(self):
        pasos = self.zt._steps_of_messages(
            [_msg("assistant", [{"type": "tool", "tool": "Grep", "state": {}}])])
        self.assertEqual(pasos, [("Grep", "")])

    async def test_reasoning_solo_assistant_y_en_orden(self):
        cot = self.zt._reasoning_of_messages(MSGS)
        self.assertIn("verificar el archivo", cot)
        self.assertIn("Ahora corro los tests", cot)
        self.assertLess(cot.index("verificar"), cot.index("Ahora corro"))
        # el texto de respuesta NO es razonamiento
        self.assertNotIn("Listo, arreglado", cot)

    async def test_reasoning_vacio_si_no_hay(self):
        self.assertEqual(self.zt._reasoning_of_messages(
            [_msg("assistant", [{"type": "text", "text": "solo texto"}])]), "")


class TestMultipart(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_cuerpo_tiene_campos_filename_y_contenido(self):
        from zcode_bot.utils.http import _multipart_body
        body, ctype = _multipart_body(
            {"chat_id": "42", "caption": "hola"}, "cot-x.txt", b"LINEA1\nLINEA2")
        self.assertIn("multipart/form-data; boundary=", ctype)
        raw = body.decode()
        self.assertIn('name="chat_id"', raw)
        self.assertIn("42", raw)
        self.assertIn('filename="cot-x.txt"', raw)
        self.assertIn("LINEA1", raw)
        self.assertTrue(raw.rstrip().endswith("--"))


class TestSendDocument(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_send_document_postea_multipart_al_bot(self):
        import urllib.request as _u
        capturado = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            capturado["url"] = req.full_url
            capturado["data"] = req.data
            capturado["ctype"] = req.headers.get("Content-type")
            return FakeResp()

        orig = _u.urlopen
        _u.urlopen = fake_urlopen
        try:
            ok = self.zt.send_document({"enabled": True, "token": "T", "chat_id": 7},
                                       "turn-x.txt", "contenido", "cap")
        finally:
            _u.urlopen = orig
        self.assertTrue(ok)
        self.assertIn("/botT/sendDocument", capturado["url"])
        self.assertIn("multipart/form-data", capturado["ctype"])
        self.assertIn(b"filename=\"turn-x.txt\"", capturado["data"])

    async def test_fallo_no_explota_y_devuelve_false(self):
        import urllib.request as _u

        def fake_urlopen(req, timeout=None):
            raise OSError("red caida")

        orig = _u.urlopen
        _u.urlopen = fake_urlopen
        try:
            ok = self.zt.send_document({"token": "T", "chat_id": 7},
                                       "x.txt", "x")
        finally:
            _u.urlopen = orig
        self.assertFalse(ok)


class TestBuildTrace(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_trace_completo_con_secciones(self):
        t = self.zt.build_trace("Mi task", "ses_x", 1.5, MSGS)
        self.assertIn("TURNO: Mi task", t)
        self.assertIn("PASOS (3)", t)
        self.assertIn("1. Read · /a/b.py", t)
        self.assertIn("2. Bash · python3 -m unittest -q", t)
        self.assertIn("RAZONAMIENTO COMPLETO", t)
        self.assertIn("verificar el archivo", t)
        self.assertIn("RESPUESTA", t)
        self.assertIn("Listo, arreglado", t)


class TestTurnTrace(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_default_no_manda_pero_persiste(self):
        # F19: el documento dejó de mandarse solo (ruido redundante con las
        # cajas de CoT) — default OFF, el archivo state queda siempre
        enviados = []
        self.zt.send_document = lambda *a, **k: enviados.append(1)
        self.zt._turn_trace({}, "T1", "ses_1", 2.0, MSGS)
        self.assertEqual(enviados, [])
        path = os.path.join(zc_config.STATE_DIR, "last-turn-trace.txt")
        self.assertTrue(os.path.exists(path))

    async def test_flag_prendido_manda_documento(self):
        # F4f: _turn_trace vive en reporting y usa uhttp.send_document —
        # el stub va al seam de transporte, no al namespace del gateway
        from unittest import mock
        from zcode_bot.utils import http as zc_http
        enviados = []
        with mock.patch.object(
                zc_http, "send_document",
                lambda cfg, fname, text, caption="":
                    (enviados.append((fname, text, caption)), True)[1]):
            self.zt._turn_trace({"trace_documents": True}, "T1", "ses_1",
                                2.0, MSGS)
        path = os.path.join(zc_config.STATE_DIR, "last-turn-trace.txt")
        with open(path) as f:
            self.assertIn("PASOS (3)", f.read())
        self.assertEqual(len(enviados), 1)
        self.assertTrue(enviados[0][0].startswith("turn-T1-"))
        self.assertIn("3 pasos", enviados[0][2])


class TestCotComando(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_sin_activa_avisa(self):
        self.zt.set_active_bot_session({})
        r = self.zt._cmd_cot({}, "")
        self.assertIn("Elegí conversación", r)

    async def test_sid_invalido_avisa(self):
        r = self.zt._cmd_cot({}, "@ses_inexistente_xyz")
        self.assertIn("No encontré", r)

    async def test_registro_menu_y_help(self):
        self.assertIsNotNone(self.zt.handle_command({}, "/cot"))
        self.assertIn("/cot", self.zt._cmd_help({}, ""))
        cap = {}

        def fake_tg_api(cfg, method, params=None, timeout=35):
            cap[method] = params

        self.zt.tg_api = fake_tg_api
        cfg = self.zt.load_config()
        cfg["chat_id"] = 1
        self.assertTrue(self.zt.register_bot_menu(cfg))
        names = [c["command"] for c in cap["setMyCommands"]["commands"]]
        self.assertIn("cot", names)


if __name__ == "__main__":
    unittest.main()
