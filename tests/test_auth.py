"""Auth del bot: from_owner fail-closed y save_offset atómico.

Lección del 15/10: from_owner era fail-open (owner_user_id None → pasaba
cualquiera) y save_offset escribía directo (un crash dejaba el offset
truncado → ValueError → offset 0 → re-procesamiento masivo).
"""
import os
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
        self.assertFalse(self.zt.from_owner(cfg, {}))  # sin "from"


class TestSaveOffsetAtomico(unittest.IsolatedAsyncioTestCase):
    async def test_escribe_y_no_deja_tmp(self):
        self.zt = support.fresh(self)
        self.zt.save_offset(12345)
        path = os.path.join(zc_config.STATE_DIR, "tg-offset")
        with open(path) as f:
            self.assertEqual(f.read(), "12345")
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertEqual(self.zt.current_offset(), 12345)


if __name__ == "__main__":
    unittest.main()
