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

    def test_runs_without_blowing_up_and_purges_old_payloads(self):
        """The NameError regression: it must run to completion without exceptions."""
        import time
        old = os.path.join(self.zt.config.LOG_DIR, "payload-20200101-000000-Stop.json")
        with open(old, "w") as f:
            f.write("{}")
        # mtime 10 days back (the payload cutoff is 2 days)
        ten_days = time.time() - 10 * 86400
        os.utime(old, (ten_days, ten_days))

        self.zt.housekeeping()  # before: NameError _log_rotate

        self.assertFalse(os.path.exists(old))

    def test_rotates_big_orphan_log(self):
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

    def test_purges_rotated_log_over_cap_and_leaves_the_small_one(self):
        """A .log.1 over LOG_ROTATED_CAP_BYTES is a flood, not a log (the
        294MB errors.log.1 of the 19/10 incident) — housekeeping deletes
        it; a normal .log.1 (below the cap) is left alone."""
        from zcode_bot.updates import LOG_ROTATED_CAP_BYTES
        log_dir = self.zt.config.LOG_DIR
        big = os.path.join(log_dir, "errors.log.1")
        with open(big, "wb") as f:
            f.seek(LOG_ROTATED_CAP_BYTES)  # sparse file: cap + 1 real byte
            f.write(b"\0")
        small = os.path.join(log_dir, "orphan.log.1")
        with open(small, "wb") as f:
            f.write(b"x")

        self.zt.housekeeping()

        self.assertFalse(os.path.exists(big))
        self.assertTrue(os.path.exists(small))

    def test_purges_transcripts_of_unreferenced_sessions(self):
        """F9: the model-io rollout of a session that is no longer active,
        nor in a turn, nor with a worker is purged past rollout_retain_dias;
        the ACTIVE session's (bot-session.json) never."""
        import time
        rollout = os.path.join(self.zt.config.BASE, "cli", "rollout")
        os.makedirs(rollout, exist_ok=True)
        old = os.path.join(rollout, "model-io-sess_VIEJA.jsonl")
        active = os.path.join(rollout,
                              "model-io-%s.jsonl" % support.T1)
        eight_days_ago = time.time() - 8 * 86400
        for p in (old, active):
            with open(p, "w") as f:
                f.write("{}")
            os.utime(p, (eight_days_ago, eight_days_ago))

        self.zt.housekeeping()

        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(active))

    def test_fresh_transcript_or_knob_at_zero_untouched(self):
        """Safety margin: the fresh transcript of today's rotation is NOT
        purged (only past the retain); rollout_retain_days=0 turns the
        purge off entirely."""
        import time
        rollout = os.path.join(self.zt.config.BASE, "cli", "rollout")
        os.makedirs(rollout, exist_ok=True)
        p = os.path.join(rollout, "model-io-sess_ROTADAHOY.jsonl")
        with open(p, "w") as f:
            f.write("{}")

        self.zt.housekeeping()
        self.assertTrue(os.path.exists(p))  # fresh: kept

        old = os.path.join(rollout, "model-io-sess_VIEJA.jsonl")
        eight_days_ago = time.time() - 8 * 86400
        with open(old, "w") as f:
            f.write("{}")
        os.utime(old, (eight_days_ago, eight_days_ago))
        cfg = self.zt.config.load_config()
        cfg["rollout_retain_days"] = 0
        self.zt.config.save_config(cfg)
        self.zt.housekeeping()
        self.assertTrue(os.path.exists(old))  # knob at 0: off


if __name__ == "__main__":
    unittest.main()
