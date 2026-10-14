"""Tests de lógica PURA (sin estado, sin red): parsing, clasificación,
callbacks de botones, formato. Congelan el contrato de los helpers.
"""
import unittest

import support


class PureHelpers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    # ---------------- _cb_text: vocabulario de callbacks ----------------

    async def test_cb_text_vocabulario(self):
        cb = self.zt._cb_text
        self.assertEqual(cb("t:sess_abc-1"), "/task @sess_abc-1")
        self.assertEqual(cb("t:ses_f02xyz"), "/task @ses_f02xyz")  # formato viejo
        self.assertEqual(cb("c:sess_abc"), "/compact @sess_abc")
        self.assertEqual(cb("c:ses_f02xyz"), "/compact @ses_f02xyz")
        self.assertEqual(cb("s:sess_abc"), "/stop @sess_abc")
        self.assertEqual(cb("m:GLM-5.3-Flash"), "/model GLM-5.3-Flash")
        self.assertEqual(cb("mo:plan"), "/mode plan")
        self.assertEqual(cb("mo:yolo"), "/mode yolo")
        self.assertEqual(cb("th:medium"), "/thinking medium")
        self.assertEqual(cb("task:2"), "/all_tasks 2")   # botones viejos → all
        self.assertEqual(cb("ans:3"), "3")
        self.assertEqual(cb("yes"), "si")
        self.assertEqual(cb("no"), "no")

    # ---------------- clasificación si/no ----------------

    async def test_classify(self):
        cl = self.zt.classify
        self.assertEqual(cl("si"), "yes")
        self.assertEqual(cl("SÍ, dale"), "yes")
        self.assertEqual(cl("aprobalo"), "yes")
        self.assertEqual(cl("no"), "no")
        self.assertEqual(cl("denegado"), "no")
        self.assertEqual(cl("hola como andas"), "text")
        self.assertEqual(cl(""), "text")

    async def test_norm(self):
        n = self.zt.norm
        self.assertEqual(n("  HÓLA   Múndo "), "hola mundo")

    # ---------------- formato ----------------

    async def test_fmt_tok(self):
        f = self.zt.fmt_tok
        self.assertEqual(f(999), "999")
        self.assertEqual(f(15800), "15.8K")
        self.assertEqual(f(17400000), "17.4M")
        self.assertEqual(f(None), "?")

    # ---------------- _compact_args: parser (zcode_compact fakeado) ----------------

    async def test_compact_args_parser(self):
        llamadas = []
        self.zt.zcode_compact = lambda rest="", n=None, sid=None, scope=None: \
            llamadas.append((rest, n, sid, scope)) or "ok"
        ca = self.zt._compact_args
        ca("3", "projects")
        ca("foco en X", "all")
        ca("@sess_zz9", "all")
        ca("@ses_aa1 instr extra", "projects")
        self.assertEqual(llamadas[0], ("", 3, None, "projects"))
        self.assertEqual(llamadas[1], ("foco en X", None, None, "all"))
        self.assertEqual(llamadas[2], ("", None, "sess_zz9", "all"))
        self.assertEqual(llamadas[3], ("instr extra", None, "ses_aa1", "projects"))

    # ---------------- _cur_sel / _set_sel ----------------

    async def test_cur_sel_sanea(self):
        cur = self.zt._cur_sel({})
        self.assertEqual(cur["modelId"], "GLM-5.3")
        self.assertEqual(cur["options"]["reasoningLevel"], "high")
        cur2 = self.zt._cur_sel({"model": {"modelId": "GLM-5.3-Flash"}})
        self.assertEqual(cur2["modelId"], "GLM-5.3-Flash")
        self.assertEqual(cur2["options"]["reasoningLevel"], "high")  # sin options

    async def test_set_sel_mutua_preservacion(self):
        # el flujo real relee la sesión activa entre comandos: idem acá
        self.zt.set_active_bot_session({"sid": "s1", "title": "t", "ws": "/w"})
        self.zt._set_sel(self.zt.active_bot_session(), model_id="GLM-5.3-Flash")
        self.zt._set_sel(self.zt.active_bot_session(), thinking="max")
        cur = self.zt._cur_sel(self.zt.active_bot_session())
        self.assertEqual(cur["modelId"], "GLM-5.3-Flash")
        self.assertEqual(cur["options"]["reasoningLevel"], "max")

    # ---------------- extract assistant text ----------------

    async def test_extract_assistant_text(self):
        msgs = [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "q"}]},
            {"info": {"role": "assistant"},
             "parts": [{"type": "reasoning", "text": "penso"},
                       {"type": "text", "text": "respuesta"},
                       {"type": "tool_use", "name": "Bash"}]},
            {"info": {"role": "assistant"},
             "parts": [{"type": "text", "text": "segunda"}]},
        ]
        self.assertEqual(self.zt._extract_assistant_text(msgs),
                         "respuesta\n\nsegunda")
        self.assertEqual(self.zt._extract_assistant_text(msgs, solo_ultima=True),
                         "segunda")


if __name__ == "__main__":
    unittest.main()
