"""Tests del Repository de tasks contra el fixture sqlite (mismos datos
que los golden: el ORDEN del sidebar está congelado por estos tests también).
"""
import os
import sys
import unittest

import support
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zcode_bot import tasks_repo as repo   # noqa: E402


class TestTasksRepo(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="zcrepo-")
        self.base = support.build_env(self.root)["base"]

    async def test_orden_sidebar_y_dedupe(self):
        ts = repo.ctx_tasks(self.base, limit=10)
        # mismo orden que los golden: MCP(pinned), testing, CV, instalaciones
        self.assertEqual([t["title"] for t in ts],
                         ["MCP", "testing", "CV", "instalaciones"])
        self.assertEqual([t["sid"] for t in ts],
                         [support.T3, support.T1, support.T2, support.T4])
        # legacy dedupe: CV aparece una sola vez pese a dos filas en el índice
        self.assertEqual(len([t for t in ts if t["title"] == "CV"]), 1)
        self.assertEqual(ts[1]["ctx"], 115412)

    async def test_limit(self):
        self.assertEqual(len(repo.ctx_tasks(self.base, limit=2)), 2)

    async def test_session_title(self):
        self.assertEqual(repo.session_title(self.base, support.T1), "testing")
        self.assertIsNone(repo.session_title(self.base, "sess_noexiste"))

    async def test_model_selection(self):
        raw = repo.model_selection(self.base, support.T2)
        self.assertIn("GLM-5.3-Flash", raw)
        payload = repo.model_selection_payload(self.base, support.T2)
        self.assertEqual(payload["options"]["reasoningLevel"], "low")
        self.assertIsNone(repo.model_selection_payload(self.base, support.T1))

    async def test_base_inexistente_devuelve_vacio(self):
        self.assertEqual(repo.ctx_tasks("/no/existe", limit=5), [])
        self.assertIsNone(repo.session_title("/no/existe", "x"))


if __name__ == "__main__":
    unittest.main()
