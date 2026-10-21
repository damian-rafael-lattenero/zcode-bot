"""Coverage gaps closed in F3: cmd_register (the flow that binds
chat/owner — zero tests before it) and the outbox (network down →
queue → resend on return)."""
import os
import time as _real_time
import types
import unittest

import support
from zcode_bot import hooks as zc_hooks          # owner of on_* (F4j)
from zcode_bot.utils import http as zc_http      # seam transporte
from zcode_bot import config as zc_config


def _cfg(zt):
    return dict(zt.DEFAULTS, token="T", chat_id=1, owner_user_id=9,
                enabled=True, loud_all=False)


class TestRegister(unittest.IsolatedAsyncioTestCase):
    async def test_first_message_registers_and_confirms(self):
        zt = support.fresh(self)
        # the fixture's config needs a token so it doesn't exit(1)
        with open(zc_config.CONFIG_PATH, "w") as f:
            f.write('{"token": "T"}')
        captured, sends = {}, []
        support.patch(self, zc_hooks, "save_config",
                        lambda cfg: captured.update(cfg))
        support.patch(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (sends.append(text), True)[1])
        batch = [{"update_id": 5, "message_id": 1, "text": "hola",
                  "chat": {"id": 42}, "from": {"id": 9,
                                               "first_name": "Dami"}}]
        calls = {"n": 0}

        def fake_fetch(cfg, offset, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return 6, batch, 1
            return 6, [], 0
        support.patch(self, zc_hooks, "fetch_updates", fake_fetch)
        zt.cmd_register()  # registers on the first batch (never reaches the deadline)
        self.assertEqual(captured.get("chat_id"), 42)
        self.assertEqual(captured.get("owner_user_id"), 9)
        self.assertEqual(captured.get("owner_name"), "Dami")
        self.assertTrue(any("Registered" in s for s in sends))


class TestOutbox(unittest.IsolatedAsyncioTestCase):
    async def test_network_down_queues_and_flush_resends(self):
        zt = support.fresh(self)
        cfg = _cfg(zt)
        from zcode_bot.utils import http as zc_http
        # send retries sleep 0/1/3s: in tests, no waiting
        support.patch(self, zc_http, "time", types.SimpleNamespace(
            sleep=lambda s: None, time=_real_time.time))

        def net_down(*a, **k):
            raise OSError("red caída")
        support.patch(self, zc_http, "tg_api", net_down)
        self.assertFalse(zc_http.send(cfg, "mensaje perdido"))
        box = zt._store("outbox.json").read()
        self.assertEqual([b["text"] for b in box], ["mensaje perdido"])

        # the network is back: the flush resends and leaves the outbox empty
        sent = []
        support.patch(self, zc_http, "tg_api",
                        lambda cfg, method, params=None, timeout=35:
                        sent.append(params["text"]) or {"ok": True})
        self.assertEqual(zc_http.outbox_flush(cfg), 1)
        self.assertEqual(sent, ["mensaje perdido"])
        self.assertEqual(zt._store("outbox.json").read() or [], [])


if __name__ == "__main__":
    unittest.main()
