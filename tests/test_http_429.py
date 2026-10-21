# -*- coding: utf-8 -*-
"""Incident 15:47 — Telegram 429: send respects the real retry_after,
sets the global cooldown, and the best-effort senders (_live_send /
_live_edit) BACK OFF during the window instead of keeping the throttle
alive by hammering (3 retries in 4s against a 30s+ punishment)."""
import fcntl
import io
import json
import unittest
import urllib.error
from unittest import mock

import support

from zcode_bot.utils import http as zc_http


def _err429(retry_after=7):
    body = json.dumps({"ok": False, "error_code": 429,
                       "parameters": {"retry_after": retry_after}}).encode()
    return urllib.error.HTTPError("url", 429, "Too Many Requests",
                                  {}, io.BytesIO(body))


class Test429(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False)
        self.calls = []
        self.addCleanup(setattr, zc_http, "_cooldown_until", [0.0])
        zc_http._cooldown_until[0] = 0.0

    def test_send_respects_retry_after_and_leaves(self):
        # 1st attempt 429 (retry_after 7) → wait that long → retry OK
        def tg_api(cfg, method, params=None, timeout=None):
            self.calls.append(method)
            if len(self.calls) == 1:
                raise _err429(7)
            return {"ok": True}

        slept = []
        with mock.patch.object(zc_http, "tg_api", tg_api), \
             mock.patch.object(zc_http, "_sleep", slept.append):
            self.assertTrue(zc_http.send(self.cfg, "hola"))
        self.assertEqual(len(self.calls), 2)
        self.assertIn(7.5, slept)
        self.assertGreater(zc_http._cooldown_until[0], 0)

    def test_persistent_429_goes_to_the_outbox(self):
        def tg_api(cfg, method, params=None, timeout=None):
            raise _err429(1)

        with mock.patch.object(zc_http, "tg_api", tg_api), \
             mock.patch.object(zc_http, "_sleep", lambda s: None):
            self.assertFalse(zc_http.send(self.cfg, "hola"))
        box = self.zt._store("outbox.json").read() or []
        self.assertEqual([b["text"] for b in box], ["hola"])

    def test_live_in_cooldown_does_not_hammer(self):
        zc_http._cooldown_until[0] = zc_http.time.time() + 999
        with mock.patch.object(zc_http, "tg_api",
                               lambda *a, **k: self.fail("golpeó")):
            self.assertIsNone(zc_http._live_send(self.cfg, "x"))
            self.assertFalse(zc_http._live_edit(self.cfg, 1, "x"))

    def test_live_edit_429_sets_cooldown_without_sleeping(self):
        slept = []

        def tg_api(cfg, method, params=None, timeout=None):
            raise _err429(4)

        with mock.patch.object(zc_http, "tg_api", tg_api), \
             mock.patch.object(zc_http, "_sleep", slept.append):
            self.assertFalse(zc_http._live_edit(self.cfg, 1, "x"))
        self.assertGreater(zc_http._cooldown_until[0], zc_http.time.time() + 3)
        self.assertEqual(slept, [])  # no sleep: the gate does the work

    def test_retry_after_parses_the_body(self):
        self.assertEqual(zc_http._retry_after(_err429(9)), 9)
        self.assertEqual(zc_http._retry_after(_err429(0)), 1)  # minimum 1
        roto = urllib.error.HTTPError("u", 429, "Too Many", {},
                                      io.BytesIO(b"no-json"))
        self.assertEqual(zc_http._retry_after(roto), 5)
        plain = ValueError("HTTP Error 429: Too Many Requests")
        self.assertEqual(zc_http._retry_after(plain), 5)  # match by str
        self.assertIsNone(zc_http._retry_after(ValueError("timeout")))


class TestOutboxFlushSinDeadlock(unittest.TestCase):
    """Incident 15:47 (part 2): outbox_flush sent INSIDE the flock and
    the failed send re-queued with ANOTHER flock on the same file —
    flock is not reentrant across fds: self-deadlock, poller frozen for
    30+ min."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False)
        self.sent = []
        self.addCleanup(setattr, zc_http, "_cooldown_until", [0.0])
        zc_http._cooldown_until[0] = 0.0

    def _store(self):
        return self.zt._store("outbox.json")

    def test_sends_outside_the_lock_and_failed_ones_return(self):
        def send(cfg, text, loud=None, markup=None, a_outbox=True):
            self.sent.append((text, a_outbox))
            return text != "malo"
        with mock.patch.object(zc_http, "send", send):
            self._store().write([{"ts": 1, "text": "uno"},
                                 {"ts": 2, "text": "malo"},
                                 {"ts": 3, "text": "tres"}])
            ok = zc_http.outbox_flush(self.cfg)
        self.assertEqual(ok, 2)
        # all attempted, all WITHOUT nested re-queueing
        self.assertEqual([t for t, _a in self.sent],
                         ["uno", "malo", "tres"])
        self.assertTrue(all(a is False for _t, a in self.sent))
        # only the failed one stayed in the queue
        box = self._store().read() or []
        self.assertEqual([b["text"] for b in box], ["malo"])

    def test_flush_send_does_not_re_enqueue_nested(self):
        # REAL send (not fake): with a_outbox=False a failure does NOT
        # touch the outbox — the nested re-queue was the deadlock
        with mock.patch.object(zc_http, "tg_api",
                               mock.Mock(side_effect=IOError("red caída"))), \
             mock.patch.object(zc_http, "_sleep", lambda s: None):
            self.assertFalse(zc_http.send(self.cfg, "x", a_outbox=False))
        self.assertEqual(self._store().read() or [], [])

    def test_flock_with_deadline_instead_of_freezing(self):
        import threading
        from zcode_bot.state import AtomicStore
        import os
        p = os.path.join(self.zt.config.STATE_DIR, "deadlock-test.json")
        st = AtomicStore(p)
        lf = open(p + ".lock", "a+")
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)  # lock held
        try:
            with self.assertRaises(OSError):
                st.update_cross_process(lambda b: b, [], timeout=0.3)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()
        # released: works
        self.assertEqual(st.update_cross_process(lambda b: {"ok": 1}, {}),
                         {"ok": 1})


if __name__ == "__main__":
    unittest.main()
