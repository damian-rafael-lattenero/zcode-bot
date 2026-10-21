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
    async def test_primer_mensaje_registra_y_confirma(self):
        zt = support.fresh(self)
        # the fixture's config needs a token so it doesn't exit(1)
        with open(zc_config.CONFIG_PATH, "w") as f:
            f.write('{"token": "T"}')
        capturado, sends = {}, []
        support.parchea(self, zc_hooks, "save_config",
                        lambda cfg: capturado.update(cfg))
        support.parchea(self, zc_http, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (sends.append(text), True)[1])
        batch = [{"update_id": 5, "message_id": 1, "text": "hola",
                  "chat": {"id": 42}, "from": {"id": 9,
                                               "first_name": "Dami"}}]
        llamadas = {"n": 0}

        def fake_fetch(cfg, offset, timeout):
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                return 6, batch, 1
            return 6, [], 0
        support.parchea(self, zc_hooks, "fetch_updates", fake_fetch)
        zt.cmd_register()  # registers on the first batch (never reaches the deadline)
        self.assertEqual(capturado.get("chat_id"), 42)
        self.assertEqual(capturado.get("owner_user_id"), 9)
        self.assertEqual(capturado.get("owner_name"), "Dami")
        self.assertTrue(any("Registered" in s for s in sends))


class TestOutbox(unittest.IsolatedAsyncioTestCase):
    async def test_red_caida_encola_y_flush_reenvia(self):
        zt = support.fresh(self)
        cfg = _cfg(zt)
        from zcode_bot.utils import http as zc_http
        # send retries sleep 0/1/3s: in tests, no waiting
        support.parchea(self, zc_http, "time", types.SimpleNamespace(
            sleep=lambda s: None, time=_real_time.time))

        def red_caida(*a, **k):
            raise OSError("red caída")
        support.parchea(self, zc_http, "tg_api", red_caida)
        self.assertFalse(zc_http.send(cfg, "mensaje perdido"))
        box = zt._store("outbox.json").read()
        self.assertEqual([b["text"] for b in box], ["mensaje perdido"])

        # the network is back: the flush resends and leaves the outbox empty
        enviados = []
        support.parchea(self, zc_http, "tg_api",
                        lambda cfg, method, params=None, timeout=35:
                        enviados.append(params["text"]) or {"ok": True})
        self.assertEqual(zc_http.outbox_flush(cfg), 1)
        self.assertEqual(enviados, ["mensaje perdido"])
        self.assertEqual(zt._store("outbox.json").read() or [], [])


if __name__ == "__main__":
    unittest.main()
