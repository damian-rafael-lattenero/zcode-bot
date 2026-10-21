"""Housekeeping (F24 + F4a): artifact purge and rotation of ALL logs.

Regression for the F4a bug: housekeeping called _log_rotate without
importing it — a NameError waiting to fire on every poller start (hidden
by the supervisor's try/except, leaving the logs unrotated forever).
"""
import os
import unittest

import support


class TestHousekeeping(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_corre_sin_explotar_y_purga_payloads_viejos(self):
        """The NameError regression: it must run to completion without exceptions."""
        import time
        viejo = os.path.join(self.zt.config.LOG_DIR, "payload-20200101-000000-Stop.json")
        with open(viejo, "w") as f:
            f.write("{}")
        # mtime 10 days back (the payload cutoff is 2 days)
        diez_dias = time.time() - 10 * 86400
        os.utime(viejo, (diez_dias, diez_dias))

        self.zt.housekeeping()  # before: NameError _log_rotate

        self.assertFalse(os.path.exists(viejo))

    def test_rota_orphan_log_grande(self):
        """orphan.log >5MB → .1 (closed the F5 pending item: only errors.log
        rotated; orphan.log grew with no ceiling)."""
        log_dir = self.zt.config.LOG_DIR
        p = os.path.join(log_dir, "orphan.log")
        with open(p, "wb") as f:
            f.seek(6 * 1024 * 1024)  # 6MB
            f.write(b"\0")

        self.zt.housekeeping()

        self.assertTrue(os.path.exists(p + ".1"))
        self.assertFalse(os.path.exists(p))

    def test_purga_log_rotado_sobre_cap_y_deja_paso_al_chico(self):
        """A .log.1 over LOG_ROTADO_CAP_BYTES is a flood, not a log (the
        294MB errors.log.1 of the 19/10 incident) — housekeeping deletes
        it; a normal .log.1 (below the cap) is left alone."""
        from zcode_bot.updates import LOG_ROTADO_CAP_BYTES
        log_dir = self.zt.config.LOG_DIR
        gordo = os.path.join(log_dir, "errors.log.1")
        with open(gordo, "wb") as f:
            f.seek(LOG_ROTADO_CAP_BYTES)  # sparse file: cap + 1 real byte
            f.write(b"\0")
        chico = os.path.join(log_dir, "orphan.log.1")
        with open(chico, "wb") as f:
            f.write(b"x")

        self.zt.housekeeping()

        self.assertFalse(os.path.exists(gordo))
        self.assertTrue(os.path.exists(chico))

    def test_purga_transcriptos_de_sesiones_no_referenciadas(self):
        """F9: the model-io rollout of a session that is no longer active,
        nor in a turn, nor with a worker is purged past rollout_retain_dias;
        the ACTIVE session's (bot-session.json) never."""
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
        """Safety margin: the fresh transcript of today's rotation is NOT
        purged (only past the retain); rollout_retain_dias=0 turns the
        purge off entirely."""
        import time
        rollout = os.path.join(self.zt.config.BASE, "cli", "rollout")
        os.makedirs(rollout, exist_ok=True)
        p = os.path.join(rollout, "model-io-sess_ROTADAHOY.jsonl")
        with open(p, "w") as f:
            f.write("{}")

        self.zt.housekeeping()
        self.assertTrue(os.path.exists(p))  # fresh: kept

        viejo = os.path.join(rollout, "model-io-sess_VIEJA.jsonl")
        hace_8_dias = time.time() - 8 * 86400
        with open(viejo, "w") as f:
            f.write("{}")
        os.utime(viejo, (hace_8_dias, hace_8_dias))
        cfg = self.zt.config.load_config()
        cfg["rollout_retain_dias"] = 0
        self.zt.config.save_config(cfg)
        self.zt.housekeeping()
        self.assertTrue(os.path.exists(viejo))  # knob at 0: off


if __name__ == "__main__":
    unittest.main()
