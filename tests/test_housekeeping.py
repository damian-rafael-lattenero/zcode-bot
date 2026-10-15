"""Housekeeping (F24 + F4a): purga de artefactos y rotación de TODOS los logs.

Regresión del bug F4a: housekeeping llamaba _log_rotate sin importarla —
NameError esperando dispararse en cada arranque del poller (lo tapaba el
try/except del supervisor, quedando los logs sin rotar para siempre).
"""
import os
import unittest

import support


class TestHousekeeping(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_corre_sin_explotar_y_purga_payloads_viejos(self):
        """La regresión del NameError: debe correr completa sin excepción."""
        import time
        viejo = os.path.join(self.zt.config.LOG_DIR, "payload-20200101-000000-Stop.json")
        with open(viejo, "w") as f:
            f.write("{}")
        # mtime a 10 días atrás (el corte de payloads es 2 días)
        diez_dias = time.time() - 10 * 86400
        os.utime(viejo, (diez_dias, diez_dias))

        self.zt.housekeeping()  # antes: NameError _log_rotate

        self.assertFalse(os.path.exists(viejo))

    def test_rota_orphan_log_grande(self):
        """orphan.log >5MB → .1 (cerraba el pendiente F5: solo errors.log
        rotaba; orphan.log crecía sin techo)."""
        log_dir = self.zt.config.LOG_DIR
        p = os.path.join(log_dir, "orphan.log")
        with open(p, "wb") as f:
            f.seek(6 * 1024 * 1024)  # 6MB
            f.write(b"\0")

        self.zt.housekeeping()

        self.assertTrue(os.path.exists(p + ".1"))
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
