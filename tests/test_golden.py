"""GOLDEN TESTS — congelan la UX actual de los comandos del bot.

Propósito (F0 del refactor): cualquier cambio de comportamiento visible se
detecta acá ANTES de llegar a producción. Si un golden rompe por una mejora
intencional, se actualiza EXPLÍCITAMENTE en el commit correspondiente.

Comandos excluidos (tocan red/runtime real): /quota, /fork, /api, /run,
/task new, y las variantes que EJECUTAN compact/stop (los "sin args" que
solo listan sí entran).
"""
import json
import os
import re
import unittest

import support


def norm(s):
    """Normaliza partes dinámicas (horas HH:MM) para comparar."""
    return re.sub(r"\d{2}:\d{2}", "HH:MM", s)


class GoldenCommands(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.env_root = self.zt.BASE

    def cmd(self, text):
        return self.zt.handle_command({}, text)

    # ---------------- listas ----------------

    def test_task_projects(self):
        r = self.cmd("/task")
        self.assertIsInstance(r, tuple)
        txt, kb = r
        self.assertEqual(
            txt,
            "💬 <b>Conversaciones por proyecto</b> — tocá una (o /task n)\n"
            "· 1. MCP · 📁haskell\n"
            "🔄 2. testing\n"
            "· 3. CV · 📁cv\n"
            "\n"
            "Texto libre que mandes va a la conversación activa (🎯).")
        botones = [f[0] for f in kb["inline_keyboard"]]
        self.assertEqual([b["text"] for b in botones],
                         ["· 1 · MCP", "🔄 2 · testing", "· 3 · CV"])
        self.assertEqual([b["callback_data"] for b in botones],
                         ["t:%s" % support.T3, "t:%s" % support.T1,
                          "t:%s" % support.T2])
        for b in botones:
            self.assertLessEqual(len(b["callback_data"]), 64)

    def test_all_tasks_incluye_sueltas(self):
        txt, kb = self.cmd("/all_tasks")
        self.assertIn("Todas las conversaciones", txt)
        self.assertIn("· 4. instalaciones · 📁default", txt)
        self.assertEqual(len(kb["inline_keyboard"]), 4)

    def test_compact_lista_projects(self):
        r = self.cmd("/compact")
        txt, kb = r
        self.assertIn("🧹 <b>Compactar — tocá una</b> (solo idle)", txt)
        self.assertIn("🔄 2. testing — 115.4K", txt)
        self.assertIn("· 1. MCP — 1.0K", txt)
        self.assertIn("· 3. CV — 132.2K", txt)
        self.assertEqual([f[0]["callback_data"].split(":")[0] for f in kb["inline_keyboard"]],
                         ["c", "c", "c"])

    def test_all_compact_lista(self):
        txt, kb = self.cmd("/all_compact")
        self.assertIn("· 4. instalaciones", txt)
        self.assertEqual(len(kb["inline_keyboard"]), 4)

    def test_stop_sin_corridas(self):
        r = self.cmd("/stop")
        self.assertEqual(
            r,
            "😌 No hay nada corriendo iniciado desde el celular.\n"
            "Lo que corre en la Mac se frena con el botón Stop de la app.")

    def test_status(self):
        r = norm(self.cmd("/status"))
        self.assertIn("🎯 Activa: «testing» · 📁bot", r)
        self.assertIn("🔄 «testing» desde HH:MM", r)

    # ---------------- model / thinking ----------------

    def test_model_default(self):
        txt, kb = self.cmd("/model")
        self.assertEqual(
            txt,
            "🎯 «testing»\n"
            "🤖 Modelo: GLM-5.3 · 🧠 thinking: high\n"
            "Tocá para cambiar (aplica en el próximo turno):")
        botones = [b["text"] for fila in kb["inline_keyboard"] for b in fila]
        self.assertEqual(botones, ["● GLM-5.3", "○ GLM-5.3-Flash"])

    def test_thinking_default(self):
        txt, kb = self.cmd("/thinking")
        self.assertEqual(
            txt,
            "🎯 «testing» · 🤖 GLM-5.3\n"
            "🧠 Thinking: high\n"
            "Tocá para cambiar (aplica en el próximo turno):")
        botones = [b["text"] for fila in kb["inline_keyboard"] for b in fila]
        self.assertEqual(botones, ["○ max", "● high", "○ low"])

    def test_model_set_preserva_thinking(self):
        r = self.cmd("/model GLM-5.3-Flash")
        self.assertIn("🤖 Modelo: GLM-5.3-Flash · 🧠 thinking: high", r)
        st = json.load(open(os.path.join(self.env_root, "hooks", "state",
                                         "bot-session.json")))
        self.assertEqual(st["model"]["modelId"], "GLM-5.3-Flash")
        self.assertEqual(st["model"]["options"]["reasoningLevel"], "high")

    def test_thinking_set_preserva_model(self):
        r = self.cmd("/thinking max")
        self.assertIn("🧠 Thinking: max · modelo GLM-5.3", r)
        r2 = self.cmd("/thinking BAJA")  # inválido
        self.assertIn("⚠️ Niveles: max · high · low", r2)

    # ---------------- selección de task ----------------

    def test_task_n_y_arroba(self):
        r = self.cmd("/task 3")  # projects: 1=MCP 2=testing 3=CV
        self.assertIn("🎯 Conversación activa: «CV»", r)
        st = json.load(open(os.path.join(self.env_root, "hooks", "state",
                                         "bot-session.json")))
        self.assertEqual(st["sid"], support.T2)
        r2 = self.cmd("/task @%s" % support.T1)
        self.assertIn("🎯 Conversación activa: «testing»", r2)

    def test_task_numero_fuera_de_lista(self):
        r = self.cmd("/task 9")
        self.assertIn("⚠️ Número fuera de la lista (1 a 3)", r)

    # ---------------- info estática ----------------

    def test_help_estructura(self):
        r = self.cmd("/help")
        for frag in ("/task — con Project", "/all_tasks", "/compact — lista Projects",
                     "/all_compact", "/stop — frená tus corridas del celular",
                     "/model — modelo de la activa · /thinking — max/high/low",
                     "/quota", "/usage", "/ctx", "/status", "/last"):
            self.assertIn(frag, r)

    def test_start_y_ping(self):
        self.assertIn("🤖 Bot de control de ZCode activo", self.cmd("/start"))
        self.assertTrue(self.cmd("/ping"))

    def test_last_trocea_y_envia(self):
        # /last devuelve None: manda los chunks directo por Telegram —
        # congelamos el troceo capturando el transporte
        enviados = []
        self.zt.send = lambda cfg, text, loud=None, markup=None: \
            enviados.append(text) or True
        r = self.cmd("/last")
        self.assertIsNone(r)
        self.assertTrue(enviados)
        self.assertIn("Última respuesta de ejemplo para /last.",
                      "".join(enviados))

    def test_usage_sin_consumo(self):
        self.assertEqual(self.cmd("/usage"),
                         "📈 Sin consumo registrado hoy todavía.")

    def test_ctx_report(self):
        r = self.cmd("/ctx")
        self.assertIn("🧠", r)
        self.assertIn("testing", r)
        self.assertIn("🧹", r)  # badge de compact del fixture

    def test_comando_desconocido(self):
        self.assertIsNone(self.cmd("/noexiste"))


if __name__ == "__main__":
    unittest.main()
