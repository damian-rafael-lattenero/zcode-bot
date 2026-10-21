"""Plan approval loop (13/10): Damian approved the plan from his phone
and the bot asked him to approve OVER AND OVER.

Causes fixed:
1. The message "⏱ Sin respuesta por Telegram: quedó el diálogo en la
   app" was ALWAYS sent — even after a successful "👍 Aprobado". The
   user believed it hadn't worked and approved again.
2. The CLI sometimes re-fires the same PermissionRequest: with no
   memory of the decision, the bot re-asked from scratch. There is
   replay now.
3. Plan approval without a feedback option: ✏️ Agregar algo al plan.
"""
import os
import sys
import unittest
from unittest import mock

import support
from zcode_bot import hooks as zc_hooks          # owner of on_* (F4j)
from zcode_bot.utils import http as zc_http      # transport seam

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


def _payload(sid=support.T1, tool="ExitPlanMode", inp=None):
    return {"session_id": sid, "tool_name": tool,
            "tool_input": inp if inp is not None else {"plan": "hacer X"}}


class _EmitSpy:
    def __init__(self, zt):
        self.salidas = []
        self.zt = zt

    def __call__(self, obj):
        self.salidas.append(obj)


class TestPermissionFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.enviados = []
        support.parchea(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.enviados.append((text, markup)), True)[1])
        self.emit = _EmitSpy(self.zt)
        support.parchea(self, zc_hooks, "emit", self.emit)

    def _aprobar(self):
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: ("yes", "si"))
        self.zt.on_permission_request(_cfg(self.zt), _payload())

    async def test_aprobado_NO_dice_sin_respuesta(self):
        # THE loop regression: after "👍 Aprobado" there must NOT follow
        # "⏱ Sin respuesta ... quedó el diálogo en la app"
        self._aprobar()
        textos = [t for t, _ in self.enviados]
        self.assertTrue(any("Aprobado" in t for t in textos))
        self.assertFalse(any("Sin respuesta" in t for t in textos))

    async def test_denegado_NO_dice_sin_respuesta(self):
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: ("no", "no"))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        textos = [t for t, _ in self.enviados]
        self.assertFalse(any("Sin respuesta" in t for t in textos))

    async def test_timeout_si_dice_sin_respuesta(self):
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        self.assertTrue(any("Sin respuesta" in t for t, _ in self.enviados))
        self.assertEqual(self.emit.salidas, [])  # no decision → no emit

    async def test_aprobacion_emite_allow(self):
        self._aprobar()
        d = self.emit.salidas[0]["hookSpecificOutput"]
        self.assertEqual(d["decision"]["behavior"], "allow")

    async def test_replay_misma_huella_no_repregunta(self):
        # the CLI re-fires the same request → same decision, no new 🔐
        self._aprobar()
        n_antes = len(self.enviados)
        support.parchea(
            self, zc_hooks, "wait_reply",
            lambda cfg, hint: (_ for _ in ()).throw(
                AssertionError("NO debe volver a esperar respuesta")))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        textos = [t for t, _ in self.enviados[n_antes:]]
        self.assertTrue(any("reenviando" in t.lower() for t in textos))
        self.assertFalse(any("Permiso requerido" in t for t in textos))
        self.assertEqual(self.emit.salidas[1]["hookSpecificOutput"]
                         ["decision"]["behavior"], "allow")

    async def test_huella_distinta_si_pregunta(self):
        self._aprobar()
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        otro = _payload(inp={"plan": "hacer OTRA COSA"})
        self.zt.on_permission_request(_cfg(self.zt), otro)
        self.assertTrue(any("Sin respuesta" in t for t, _ in self.enviados))


class TestPlanAprobacion(unittest.IsolatedAsyncioTestCase):
    """🗺 ExitPlanMode: Aprobar / Rechazar / ✏️ Agregar algo buttons."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.enviados = []
        support.parchea(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.enviados.append((text, markup)), True)[1])
        self.emit = _EmitSpy(self.zt)
        support.parchea(self, zc_hooks, "emit", self.emit)

    async def test_botones_de_plan(self):
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        texto, markup = self.enviados[0]
        self.assertIn("Plan listo para aprobar", texto)
        botones = [b for row in markup["inline_keyboard"] for b in row]
        datas = [b["callback_data"] for b in botones]
        self.assertIn("yes", datas)
        self.assertIn("no", datas)
        self.assertIn("planfix", datas)  # ✏️ add something to the plan
        labels = " ".join(b["text"] for b in botones)
        self.assertIn("Agregar algo al plan", labels)

    async def test_planfix_rechaza_con_feedback(self):
        respuestas = [("text", "planfix"), ("text", "cambiá la fase 2")]
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: respuestas.pop(0))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        d = self.emit.salidas[0]["hookSpecificOutput"]
        self.assertEqual(d["decision"]["behavior"], "deny")
        self.assertIn("cambiá la fase 2", d["decision"]["message"])
        self.assertTrue(any("Ajustes enviados" in t for t, _ in self.enviados))

    async def test_planfix_sin_texto_queda_pendiente(self):
        respuestas = [("text", "planfix"), ("/no", "/no")]
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: respuestas.pop(0))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        self.assertEqual(self.emit.salidas, [])
        self.assertTrue(any("pendiente" in t for t, _ in self.enviados))

    async def test_permiso_normal_no_muestra_botones_de_plan(self):
        support.parchea(self, zc_hooks, "wait_reply",
                        lambda cfg, hint: (None, None))
        self.zt.on_permission_request(_cfg(self.zt),
                                      _payload(tool="Bash", inp={"command": "ls"}))
        texto, markup = self.enviados[0]
        self.assertIn("Permiso requerido", texto)
        self.assertNotIn("Plan listo", texto)
        datas = [b["callback_data"] for row in markup["inline_keyboard"]
                 for b in row]
        self.assertNotIn("planfix", datas)


if __name__ == "__main__":
    unittest.main()
