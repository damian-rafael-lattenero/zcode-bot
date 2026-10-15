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

    def test_purga_transcriptos_de_sesiones_no_referenciadas(self):
        """F9: el rollout model-io de una sesión que ya no está ni activa
        ni con turno ni con worker se purga pasado rollout_retain_dias; el
        de la sesión ACTIVA (bot-session.json) jamás."""
        import time
        rollout = os.path.join(self.zt.config.BASE, "cli", "rollout")
        os.makedirs(rollout, exist_ok=True)
        viejo = os.path.join(rollout, "model-io-sess_VIEJA.jsonl")
        activo = os.path.join(rollout,
                              "model-io-%s.jsonl" % support.T1)
        hace_8_dias = time.time() - 8 * 86400
        for p in (viejo, activo):
            with open(p, "w") as f:
                f.write("{}")
            os.utime(p, (hace_8_dias, hace_8_dias))

        self.zt.housekeeping()

        self.assertFalse(os.path.exists(viejo))
        self.assertTrue(os.path.exists(activo))

    def test_transcripto_fresco_o_knob_en_0_no_se_tocan(self):
        """Margen de seguridad: el transcripto reciente de una rotación de
        hoy NO se purga (solo pasado el retain); rollout_retain_dias=0
        apaga toda la purga."""
        import time
        rollout = os.path.join(self.zt.config.BASE, "cli", "rollout")
        os.makedirs(rollout, exist_ok=True)
        p = os.path.join(rollout, "model-io-sess_ROTADAHOY.jsonl")
        with open(p, "w") as f:
            f.write("{}")

        self.zt.housekeeping()
        self.assertTrue(os.path.exists(p))  # fresco: queda

        viejo = os.path.join(rollout, "model-io-sess_VIEJA.jsonl")
        hace_8_dias = time.time() - 8 * 86400
        with open(viejo, "w") as f:
            f.write("{}")
        os.utime(viejo, (hace_8_dias, hace_8_dias))
        cfg = self.zt.config.load_config()
        cfg["rollout_retain_dias"] = 0
        self.zt.config.save_config(cfg)
        self.zt.housekeeping()
        self.assertTrue(os.path.exists(viejo))  # knob en 0: off


if __name__ == "__main__":
    unittest.main()
