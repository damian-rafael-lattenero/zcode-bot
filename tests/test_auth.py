"""Bot auth: fail-closed from_owner and atomic save_offset.

Lesson from 15/10: from_owner was fail-open (owner_user_id None →
anyone passed) and save_offset wrote directly (a crash left the offset
truncated → ValueError → offset 0 → massive reprocessing).
"""
import os
import subprocess
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))
from zcode_bot import config as zc_config  # noqa: E402

import support  # noqa: E402


class TestFromOwner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_sin_owner_deniega_a_cualquiera(self):
        cfg = {"owner_user_id": None}
        self.assertFalse(self.zt.from_owner(cfg, {"from": {"id": 666}}))

    async def test_owner_registrado_solo_pasa_el_dueno(self):
        cfg = {"owner_user_id": 9}
        self.assertTrue(self.zt.from_owner(cfg, {"from": {"id": 9}}))
        self.assertFalse(self.zt.from_owner(cfg, {"from": {"id": 666}}))
        self.assertFalse(self.zt.from_owner(cfg, {}))  # no "from"


class TestSaveOffsetAtomico(unittest.IsolatedAsyncioTestCase):
    async def test_escribe_y_no_deja_tmp(self):
        self.zt = support.fresh(self)
        self.zt.save_offset(12345)
        path = os.path.join(zc_config.STATE_DIR, "tg-offset")
        with open(path) as f:
            self.assertEqual(f.read(), "12345")
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertEqual(self.zt.current_offset(), 12345)

    def _tmps(self):
        return [n for n in os.listdir(zc_config.STATE_DIR)
                if n.startswith("tg-offset.")]

    async def test_el_tmp_lleva_el_pid(self):
        """20/10 18:10: two writers at once (the poller and another
        getUpdates client — cmd_register or the drain) shared
        'tg-offset.tmp': the loser did os.replace over a tmp the other
        had already moved (ENOENT in errors.log). With the pid in the
        name there is no shared tmp."""
        self.zt = support.fresh(self)
        vistos = []
        real = os.replace
        support.parchea(self, os, "replace",
                        lambda src, dst: (vistos.append(src),
                                          real(src, dst))[1])
        self.zt.save_offset(7)
        path = os.path.join(zc_config.STATE_DIR, "tg-offset")
        self.assertEqual([s for s in vistos if "tg-offset" in s],
                         ["%s.tmp.%d" % (path, os.getpid())])
        self.assertEqual(self.zt.current_offset(), 7)
        self.assertEqual(self._tmps(), [])

    async def test_dos_procesos_escribiendo_a_la_vez_no_chocan(self):
        # the other writer is a real PROCESS over the SAME state dir
        self.zt = support.fresh(self)
        n = 60
        src = os.path.join(TESTS_DIR, "..", "src")
        otro = subprocess.Popen([sys.executable, "-c", (
            "import sys; sys.path.insert(0, %r)\n"
            "from zcode_bot import config; config.STATE_DIR = %r\n"
            "from zcode_bot import updates\n"
            "for i in range(%d): updates.save_offset(1000 + i)\n")
            % (src, zc_config.STATE_DIR, n)])
        for i in range(n):
            self.zt.save_offset(i)  # with a fixed tmp name this used to hit ENOENT
        self.assertEqual(otro.wait(timeout=60), 0)
        self.assertIsInstance(self.zt.current_offset(), int)
        self.assertEqual(self._tmps(), [])


if __name__ == "__main__":
    unittest.main()
