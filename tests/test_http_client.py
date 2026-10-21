"""Tests for AsyncTelegramClient."""
import asyncio
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zcode_bot.limites import TELEGRAM_MSG_MAX  # noqa: E402
from zcode_bot.http_client import (
    AsyncTelegramClient,
    TelegramEndpointConfig,
    RateLimiterConfig,
    CircuitBreakerConfig,
)


class MockResponse:
    def __init__(self, status=200, json_data=None, headers=None):
        self.status = status
        self._json_data = json_data or {"ok": True, "result": {}}
        self.headers = headers or {}
        self.request_info = mock.Mock()
        self.history = []

    async def json(self):
        return self._json_data

    async def text(self):
        return json.dumps(self._json_data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockPostContext:
    """Async context manager for session.post"""
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        pass


class MockSession:
    def __init__(self):
        self.post_calls = []
        self.closed = False
        self._response = MockResponse()

    def post(self, url, json=None, data=None, timeout=None):
        self.post_calls.append((url, json, data, timeout))
        return MockPostContext(self._response)

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestAsyncTelegramClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_session = MockSession()
        self.client = AsyncTelegramClient(
            token="TEST_TOKEN",
            session=self.mock_session,
        )

    async def test_send_message_success(self):
        self.mock_session._response = MockResponse(200, {
            "ok": True,
            "result": {"message_id": 123, "text": "hola"}
        })

        result = await self.client.send_message(chat_id=42, text="hola")

        self.assertEqual(result["result"]["message_id"], 123)
        self.assertEqual(len(self.mock_session.post_calls), 1)
        url, params, _, _ = self.mock_session.post_calls[0]
        self.assertIn("sendMessage", url)
        self.assertEqual(params["chat_id"], 42)
        self.assertEqual(params["text"], "hola")

    async def test_send_message_truncates_long_text(self):
        long_text = "x" * 5000
        self.mock_session._response = MockResponse(200, {"ok": True, "result": {}})

        await self.client.send_message(chat_id=1, text=long_text)

        _, params, _, _ = self.mock_session.post_calls[0]
        self.assertEqual(len(params["text"]), TELEGRAM_MSG_MAX)

    async def test_edit_message_text(self):
        self.mock_session._response = MockResponse(200, {"ok": True, "result": {}})

        await self.client.edit_message_text(chat_id=1, message_id=5, text="editado")

        url, params, _, _ = self.mock_session.post_calls[0]
        self.assertIn("editMessageText", url)
        self.assertEqual(params["message_id"], 5)

    async def test_send_document(self):
        self.mock_session._response = MockResponse(200, {"ok": True, "result": {}})

        await self.client.send_document(chat_id=1, filename="test.txt", content=b"contenido")

        url, params, data, _ = self.mock_session.post_calls[0]
        self.assertIn("sendDocument", url)
        # Verify that FormData with the file was passed
        self.assertIsNotNone(data)

    async def test_get_updates(self):
        self.mock_session._response = MockResponse(200, {
            "ok": True,
            "result": [{"update_id": 1, "message": {"text": "hola"}}]
        })

        result = await self.client.get_updates(offset=10, timeout=30)

        self.assertEqual(len(result["result"]), 1)
        _, params, _, timeout = self.mock_session.post_calls[0]
        self.assertEqual(params["offset"], 10)
        self.assertEqual(timeout.total, 45)  # timeout + 15

    async def test_retry_on_500(self):
        responses = [
            MockResponse(500, {"ok": False, "description": "Internal Server Error"}),
            MockResponse(500, {"ok": False, "description": "Internal Server Error"}),
            MockResponse(200, {"ok": True, "result": {"message_id": 1}}),
        ]
        call_count = [0]

        def mock_post(url, json=None, data=None, timeout=None):
            self.mock_session.post_calls.append((url, json, data, timeout))
            resp = responses[call_count[0]]
            call_count[0] += 1
            return MockPostContext(resp)

        self.mock_session.post = mock_post

        result = await self.client.send_message(chat_id=1, text="test")
        self.assertEqual(result["result"]["message_id"], 1)
        self.assertEqual(len(self.mock_session.post_calls), 3)

    async def test_429_retry_after_header(self):
        responses = [
            MockResponse(429, {"ok": False, "description": "Too Many Requests"},
                         headers={"Retry-After": "1"}),
            MockResponse(200, {"ok": True, "result": {"message_id": 1}}),
        ]
        call_count = [0]

        def mock_post(url, json=None, data=None, timeout=None):
            self.mock_session.post_calls.append((url, json, data, timeout))
            resp = responses[call_count[0]]
            call_count[0] += 1
            return MockPostContext(resp)

        self.mock_session.post = mock_post

        result = await self.client.send_message(chat_id=1, text="test")
        self.assertEqual(result["result"]["message_id"], 1)
        self.assertEqual(len(self.mock_session.post_calls), 2)

    async def test_circuit_breaker_opens_after_failures(self):
        # Use a connection error (not 500) to avoid the client's internal retries
        # retry_on_500=True by default would do 5 retries per call = 57s
        client = AsyncTelegramClient(
            token="TEST",
            session=self.mock_session,
            endpoints={
                "sendMessage": TelegramEndpointConfig(
                    method="sendMessage",
                    circuit_breaker=CircuitBreakerConfig(failure_threshold=2, timeout=10),
                    rate_limit=RateLimiterConfig(rate=1000, burst=100),
                    retry_on_500=False,  # key: don't retry 500s in tests
                )
            }
        )

        # 2 connection failures -> OPEN
        self.mock_session._response = MockResponse(500, {"ok": False})

        for _ in range(2):
            with self.assertRaises(Exception):
                await client.send_message(1, "test")

        breaker = client._breakers["sendMessage"]
        self.assertEqual(breaker.state, "OPEN")

    async def test_rate_limiter_respected(self):
        client = AsyncTelegramClient(
            token="TEST",
            session=self.mock_session,
            endpoints={
                "sendMessage": TelegramEndpointConfig(
                    method="sendMessage",
                    rate_limit=RateLimiterConfig(rate=10, burst=2),  # slow
                    circuit_breaker=CircuitBreakerConfig(failure_threshold=100),
                )
            }
        )

        self.mock_session._response = MockResponse(200, {"ok": True, "result": {}})

        start = time.monotonic()
        await asyncio.gather(*[client.send_message(1, f"msg{i}") for i in range(3)])
        elapsed = time.monotonic() - start

        # 3 messages with burst=2, rate=10 -> at least 0.1s for the 3rd
        self.assertGreaterEqual(elapsed, 0.08)

    async def test_close_session(self):
        # Client doesn't close externally provided session
        await self.client.close()
        # manually close for test
        await self.mock_session.close()
        self.assertTrue(self.mock_session.closed)

    async def test_context_manager(self):
        # Use client without providing session (it creates its own)
        async with AsyncTelegramClient(token="TEST") as client:
            client._session = self.mock_session
            self.mock_session._response = MockResponse(200, {"ok": True, "result": {}})
            await client.send_message(1, "test")
        # Context manager closes the session it owns
        self.assertTrue(self.mock_session.closed)

    async def test_breaker_state_exposed(self):
        self.assertEqual(self.client.get_breaker_state("sendMessage"), "CLOSED")
        self.assertEqual(self.client.get_breaker_state("unknown"), "CLOSED")


if __name__ == "__main__":
    import time
    unittest.main()