"""Loop de aprobación de planes (13/10): Damian aprobaba el plan desde el
celular y el bot le pedía aprobar UNA Y OTRA VEZ.

Causas arregladas:
1. El mensaje "⏱ Sin respuesta por Telegram: quedó el diálogo en la app"
   se mandaba SIEMPRE — incluso después de un "👍 Aprobado" exitoso. El
   usuario creía que no había funcionado y aprobaba de nuevo.
2. El CLI a veces re-dispara el mismo PermissionRequest: sin memoria de
   la decisión, el bot re-preguntaba desde cero. Ahora hay replay.
3. Aprobación de plan sin opción de feedback: ✏️ Agregar algo al plan.
"""
import os
import sys
import unittest
from unittest import mock

import support
from zcode_bot import hooks as zc_hooks          # dueño de on_* (F4j)
from zcode_bot.utils import http as zc_http      # seam transporte

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
        self.addCleanup(setattr, zc_http, "send", zc_http.send)
        zc_http.send = lambda cfg, text, loud=None, markup=None: (
            self.enviados.append((text, markup)), True)[1]
        self.emit = _EmitSpy(self.zt)
        zc_hooks.emit = self.emit

    def _aprobar(self):
        zc_hooks.wait_reply = lambda cfg, hint: ("yes", "si")
        self.zt.on_permission_request(_cfg(self.zt), _payload())

    async def test_aprobado_NO_dice_sin_respuesta(self):
        # LA regresión del loop: tras "👍 Aprobado" NO puede venir
        # "⏱ Sin respuesta ... quedó el diálogo en la app"
        self._aprobar()
        textos = [t for t, _ in self.enviados]
        self.assertTrue(any("Aprobado" in t for t in textos))
        self.assertFalse(any("Sin respuesta" in t for t in textos))

    async def test_denegado_NO_dice_sin_respuesta(self):
        zc_hooks.wait_reply = lambda cfg, hint: ("no", "no")
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        textos = [t for t, _ in self.enviados]
        self.assertFalse(any("Sin respuesta" in t for t in textos))

    async def test_timeout_si_dice_sin_respuesta(self):
        zc_hooks.wait_reply = lambda cfg, hint: (None, None)
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        self.assertTrue(any("Sin respuesta" in t for t, _ in self.enviados))
        self.assertEqual(self.emit.salidas, [])  # sin decisión → no emite

    async def test_aprobacion_emite_allow(self):
        self._aprobar()
        d = self.emit.salidas[0]["hookSpecificOutput"]
        self.assertEqual(d["decision"]["behavior"], "allow")

    async def test_replay_misma_huella_no_repregunta(self):
        # el CLI re-dispara el mismo pedido → misma decisión, sin 🔐 nuevo
        self._aprobar()
        n_antes = len(self.enviados)
        zc_hooks.wait_reply = lambda cfg, hint: (_ for _ in ()).throw(
            AssertionError("NO debe volver a esperar respuesta"))
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        textos = [t for t, _ in self.enviados[n_antes:]]
        self.assertTrue(any("reenviando" in t.lower() for t in textos))
        self.assertFalse(any("Permiso requerido" in t for t in textos))
        self.assertEqual(self.emit.salidas[1]["hookSpecificOutput"]
                         ["decision"]["behavior"], "allow")

    async def test_huella_distinta_si_pregunta(self):
        self._aprobar()
        zc_hooks.wait_reply = lambda cfg, hint: (None, None)
        otro = _payload(inp={"plan": "hacer OTRA COSA"})
        self.zt.on_permission_request(_cfg(self.zt), otro)
        self.assertTrue(any("Sin respuesta" in t for t, _ in self.enviados))


class TestPlanAprobacion(unittest.IsolatedAsyncioTestCase):
    """🗺 ExitPlanMode: botones Aprobar / Rechazar / ✏️ Agregar algo."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.enviados = []
        self.addCleanup(setattr, zc_http, "send", zc_http.send)
        zc_http.send = lambda cfg, text, loud=None, markup=None: (
            self.enviados.append((text, markup)), True)[1]
        self.emit = _EmitSpy(self.zt)
        zc_hooks.emit = self.emit

    async def test_botones_de_plan(self):
        zc_hooks.wait_reply = lambda cfg, hint: (None, None)
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        texto, markup = self.enviados[0]
        self.assertIn("Plan listo para aprobar", texto)
        botones = [b for row in markup["inline_keyboard"] for b in row]
        datas = [b["callback_data"] for b in botones]
        self.assertIn("yes", datas)
        self.assertIn("no", datas)
        self.assertIn("planfix", datas)  # ✏️ agregar algo al plan
        labels = " ".join(b["text"] for b in botones)
        self.assertIn("Agregar algo al plan", labels)

    async def test_planfix_rechaza_con_feedback(self):
        respuestas = [("text", "planfix"), ("text", "cambiá la fase 2")]
        zc_hooks.wait_reply = lambda cfg, hint: respuestas.pop(0)
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        d = self.emit.salidas[0]["hookSpecificOutput"]
        self.assertEqual(d["decision"]["behavior"], "deny")
        self.assertIn("cambiá la fase 2", d["decision"]["message"])
        self.assertTrue(any("Ajustes enviados" in t for t, _ in self.enviados))

    async def test_planfix_sin_texto_queda_pendiente(self):
        respuestas = [("text", "planfix"), ("/no", "/no")]
        zc_hooks.wait_reply = lambda cfg, hint: respuestas.pop(0)
        self.zt.on_permission_request(_cfg(self.zt), _payload())
        self.assertEqual(self.emit.salidas, [])
        self.assertTrue(any("pendiente" in t for t, _ in self.enviados))

    async def test_permiso_normal_no_muestra_botones_de_plan(self):
        zc_hooks.wait_reply = lambda cfg, hint: (None, None)
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
