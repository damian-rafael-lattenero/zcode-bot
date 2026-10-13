"""F7: trace de turnos — paso a paso + CoT completo en Telegram.

Congela: extractores de steps/reasoning del historial, cuerpo multipart de
sendDocument, build_trace, _turn_trace (persistencia + flag) y presencia de
/cot en registro/menú/help.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import support


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


class TestExtractores(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_steps_de_tools_con_command_y_file(self):
        pasos = self.zt._steps_of_messages(MSGS)
        self.assertEqual(pasos, [
            ("Read", "/a/b.py"),
            ("Bash", "python3 -m unittest -q"),
            ("Edit", "/a/b.py"),
        ])

    def test_steps_tolerante_sin_input(self):
        pasos = self.zt._steps_of_messages(
            [_msg("assistant", [{"type": "tool", "tool": "Grep", "state": {}}])])
        self.assertEqual(pasos, [("Grep", "")])

    def test_reasoning_solo_assistant_y_en_orden(self):
        cot = self.zt._reasoning_of_messages(MSGS)
        self.assertIn("verificar el archivo", cot)
        self.assertIn("Ahora corro los tests", cot)
        self.assertLess(cot.index("verificar"), cot.index("Ahora corro"))
        # el texto de respuesta NO es razonamiento
        self.assertNotIn("Listo, arreglado", cot)

    def test_reasoning_vacio_si_no_hay(self):
        self.assertEqual(self.zt._reasoning_of_messages(
            [_msg("assistant", [{"type": "text", "text": "solo texto"}])]), "")


class TestMultipart(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_cuerpo_tiene_campos_filename_y_contenido(self):
        body, ctype = self.zt._multipart_body(
            {"chat_id": "42", "caption": "hola"}, "cot-x.txt", b"LINEA1\nLINEA2")
        self.assertIn("multipart/form-data; boundary=", ctype)
        raw = body.decode()
        self.assertIn('name="chat_id"', raw)
        self.assertIn("42", raw)
        self.assertIn('filename="cot-x.txt"', raw)
        self.assertIn("LINEA1", raw)
        self.assertTrue(raw.rstrip().endswith("--"))


class TestSendDocument(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_send_document_postea_multipart_al_bot(self):
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

    def test_fallo_no_explota_y_devuelve_false(self):
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


class TestBuildTrace(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_trace_completo_con_secciones(self):
        t = self.zt.build_trace("Mi task", "ses_x", 1.5, MSGS)
        self.assertIn("TURNO: Mi task", t)
        self.assertIn("PASOS (3)", t)
        self.assertIn("1. Read · /a/b.py", t)
        self.assertIn("2. Bash · python3 -m unittest -q", t)
        self.assertIn("RAZONAMIENTO COMPLETO", t)
        self.assertIn("verificar el archivo", t)
        self.assertIn("RESPUESTA", t)
        self.assertIn("Listo, arreglado", t)


class TestTurnTrace(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_persiste_y_manda_documento(self):
        enviados = []
        self.zt.send_document = lambda cfg, fname, text, caption="": (
            enviados.append((fname, text, caption)), True)[1]

        self.zt._turn_trace({}, "T1", "ses_1", 2.0, MSGS)
        path = os.path.join(self.zt.STATE_DIR, "last-turn-trace.txt")
        with open(path) as f:
            self.assertIn("PASOS (3)", f.read())
        self.assertEqual(len(enviados), 1)
        self.assertTrue(enviados[0][0].startswith("turn-T1-"))
        self.assertIn("3 pasos", enviados[0][2])

    def test_flag_apaga_documento_pero_persiste(self):
        enviados = []
        self.zt.send_document = lambda *a, **k: enviados.append(1)
        self.zt._turn_trace({"trace_documents": False}, "T", "s", 1.0, MSGS)
        self.assertEqual(enviados, [])
        path = os.path.join(self.zt.STATE_DIR, "last-turn-trace.txt")
        self.assertTrue(os.path.exists(path))


class TestCotComando(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_sin_activa_avisa(self):
        self.zt.set_active_bot_session({})
        r = self.zt._cmd_cot({}, "")
        self.assertIn("Elegí conversación", r)

    def test_sid_invalido_avisa(self):
        r = self.zt._cmd_cot({}, "@ses_inexistente_xyz")
        self.assertIn("No encontré", r)

    def test_registro_menu_y_help(self):
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
