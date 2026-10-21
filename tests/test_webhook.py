"""Webhook receiver contract (F3): fail-closed auth by secret token
(constant-time comparison), 200 after queueing to the dispatcher, dedup
by update_id, garbage JSON → 400. No real network: aiohttp TestServer."""
import asyncio
import unittest

import support  # sys.path of src (deterministic)
from aiohttp.test_utils import TestClient, TestServer

from zcode_bot import webhook_server
from zcode_bot.dispatcher import UpdateDispatcher


def _update(uid, text="hola"):
    return {"update_id": uid, "message": {"message_id": uid, "text": text,
                                          "chat": {"id": 1},
                                          "from": {"id": 9}}}


class TestWebhookContract(unittest.IsolatedAsyncioTestCase):

    def _build(self, secret="s3cret", owner_ok=True):
        self.cfg = {"webhook_secret": secret}
        self.procesados = []
        self.dispatcher = UpdateDispatcher(
            lambda cfg, m: self.procesados.append(m),
            lambda cfg, m: owner_ok)
        self.consumer = asyncio.ensure_future(self.dispatcher.run())
        app = webhook_server.make_app(
            self.cfg, self.dispatcher,
            # like the real normalize_update: stamps update_id (dedup)
            lambda cfg, u: dict(u.get("message") or {},
                                update_id=u.get("update_id"))
            if u.get("message") else None,
            lambda: self.cfg.get("webhook_secret"))
        self.client = TestClient(TestServer(app))

    async def asyncSetUp(self):
        pass  # each test calls _build with its own config

    async def _start(self):
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.consumer.cancel)

    async def _post(self, update, token=None):
        import json as _json
        headers = {}
        if token is not None:
            headers["X-Telegram-Bot-Api-Secret-Token"] = token
        resp = await self.client.post("/telegram", json=update,
                                      headers=headers)
        raw = await resp.text()
        try:
            body = _json.loads(raw)
        except ValueError:
            body = raw  # 401/400 respond text/plain
        return resp.status, body

    async def test_sin_token_401(self):
        self._build()
        await self._start()
        status, _ = await self._post(_update(1), token=None)
        self.assertEqual(status, 401)

    async def test_token_malo_401(self):
        self._build()
        await self._start()
        status, _ = await self._post(_update(1), token="otro-secret")
        self.assertEqual(status, 401)
        await self.dispatcher.drain()
        self.assertEqual(self.procesados, [])

    async def test_sin_secret_configurado_401_a_todo(self):
        # fail-closed: a deploy without webhook_secret leaves no door open
        self._build(secret=None)
        await self._start()
        status, _ = await self._post(_update(1), token="lo-que-sea")
        self.assertEqual(status, 401)

    async def test_json_basura_400(self):
        self._build()
        await self._start()
        resp = await self.client.post(
            "/telegram", data="no-soy-json", headers={
                "X-Telegram-Bot-Api-Secret-Token": "s3cret",
                "Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)

    async def test_update_valido_200_y_procesa_uno(self):
        self._build()
        await self._start()
        status, body = await self._post(_update(1), token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(body["motivo"], "ok")
        await self.dispatcher.drain()
        self.assertEqual(len(self.procesados), 1)
        self.assertEqual(self.procesados[0]["text"], "hola")

    async def test_reintento_duplicado_200_y_no_reprocesa(self):
        # Telegram retries on timeout: dedup makes it idempotent
        self._build()
        await self._start()
        s1, b1 = await self._post(_update(7), token="s3cret")
        s2, b2 = await self._post(_update(7), token="s3cret")
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(b1["motivo"], "ok")
        self.assertEqual(b2["motivo"], "duplicado")
        await self.dispatcher.drain()
        self.assertEqual(len(self.procesados), 1)

    async def test_no_owner_200_pero_no_procesa(self):
        self._build(owner_ok=False)
        await self._start()
        status, body = await self._post(_update(3), token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(body["motivo"], "no-owner")
        await self.dispatcher.drain()
        self.assertEqual(self.procesados, [])

    async def test_health_y_ready(self):
        self._build()
        await self._start()
        r1 = await self.client.get("/health")
        r2 = await self.client.get("/ready")
        self.assertEqual(r1.status, 200)
        self.assertEqual(r2.status, 200)


class TestNormalizeViaWebhook(unittest.IsolatedAsyncioTestCase):
    """The polling==webhook equivalence: a Telegram callback_query goes
    through the SAME normalize_update (button → equivalent text)."""

    async def test_callback_baja_como_pseudo_mensaje(self):
        zt = support.fresh(self)

        class _FakeDispatcher:
            def __init__(self):
                self.recibidos = []

            async def submit(self, cfg, msg):
                self.recibidos.append(msg)
                return True, "ok"

        fake = _FakeDispatcher()
        cfg = {"webhook_secret": "s"}
        app = webhook_server.make_app(
            cfg, fake, zt.normalize_update, lambda: cfg.get("webhook_secret"))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)

        update = {"update_id": 11, "callback_query": {
            "id": "cb1", "data": "t:sess_TEST00000000000000000000001",
            "from": {"id": 9},
            "message": {"message_id": 5, "chat": {"id": 1}}}}
        resp = await client.post("/telegram", json=update, headers={
            "X-Telegram-Bot-Api-Secret-Token": "s"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(fake.recibidos), 1)
        msg = fake.recibidos[0]
        self.assertEqual(msg["text"],
                         "/task @sess_TEST00000000000000000000001")
        self.assertEqual(msg["_cb_data"],
                         "t:sess_TEST00000000000000000000001")
        self.assertEqual(msg["update_id"], 11)  # dedup available


if __name__ == "__main__":
    unittest.main()
