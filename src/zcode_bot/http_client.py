"""Async HTTP Client para Telegram Bot API — circuit breaker + rate limiter + retries."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Dict

import aiohttp

from zcode_bot.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpen
from zcode_bot.rate_limiter import AsyncRateLimiter, RateLimiterConfig

logger = logging.getLogger(__name__)


@dataclass
class TelegramEndpointConfig:
    """Configuración por endpoint de Telegram."""
    method: str
    rate_limit: RateLimiterConfig = field(default_factory=RateLimiterConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    max_retries: int = 5
    base_timeout: float = 35.0
    retry_on_429: bool = True
    retry_on_500: bool = True


DEFAULT_ENDPOINTS = {
    "sendMessage": TelegramEndpointConfig(
        method="sendMessage",
        rate_limit=RateLimiterConfig(rate=30, burst=5),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=5, timeout=30),
    ),
    "editMessageText": TelegramEndpointConfig(
        method="editMessageText",
        rate_limit=RateLimiterConfig(rate=30, burst=5),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=5, timeout=30),
    ),
    "sendDocument": TelegramEndpointConfig(
        method="sendDocument",
        rate_limit=RateLimiterConfig(rate=10, burst=2),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, timeout=60),
        max_retries=3,
        base_timeout=90.0,
    ),
    "getUpdates": TelegramEndpointConfig(
        method="getUpdates",
        rate_limit=RateLimiterConfig(rate=1, burst=1),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, timeout=60),
        max_retries=3,
        base_timeout=40.0,
    ),
    "setMyCommands": TelegramEndpointConfig(
        method="setMyCommands",
        rate_limit=RateLimiterConfig(rate=1, burst=1),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, timeout=60),
    ),
    "answerCallbackQuery": TelegramEndpointConfig(
        method="answerCallbackQuery",
        rate_limit=RateLimiterConfig(rate=30, burst=10),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=10, timeout=30),
    ),
    "setMessageReaction": TelegramEndpointConfig(
        method="setMessageReaction",
        rate_limit=RateLimiterConfig(rate=30, burst=10),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=10, timeout=30),
    ),
    "sendChatAction": TelegramEndpointConfig(
        method="sendChatAction",
        rate_limit=RateLimiterConfig(rate=30, burst=10),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=10, timeout=30),
    ),
    "setWebhook": TelegramEndpointConfig(
        method="setWebhook",
        rate_limit=RateLimiterConfig(rate=1, burst=1),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, timeout=60),
    ),
    "deleteWebhook": TelegramEndpointConfig(
        method="deleteWebhook",
        rate_limit=RateLimiterConfig(rate=1, burst=1),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, timeout=60),
    ),
    "getWebhookInfo": TelegramEndpointConfig(
        method="getWebhookInfo",
        rate_limit=RateLimiterConfig(rate=1, burst=1),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, timeout=60),
    ),
}


class AsyncTelegramClient:
    """Cliente async para Telegram Bot API con circuit breaker, rate limiter y retries."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.telegram.org",
        endpoints: Optional[dict[str, TelegramEndpointConfig]] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.endpoints = endpoints or DEFAULT_ENDPOINTS
        self._session = session
        self._own_session = session is None

        # Circuit breakers por endpoint
        self._breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(name, cfg.circuit_breaker)
            for name, cfg in self.endpoints.items()
        }

        # Rate limiters por endpoint
        self._limiters: dict[str, AsyncRateLimiter] = {
            name: AsyncRateLimiter(cfg.rate_limit)
            for name, cfg in self.endpoints.items()
        }

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _url(self, method: str) -> str:
        return f"{self.base_url}/bot{self.token}/{method}"

    async def post(
        self,
        method: str,
        params: dict[str, Any],
        timeout: Optional[float] = None,
        files: Optional[dict] = None,
    ) -> dict[str, Any]:
        """POST a un endpoint de Telegram con circuit breaker + rate limit + retries."""
        endpoint_cfg = self.endpoints.get(method)
        if not endpoint_cfg:
            raise ValueError(f"Unknown endpoint: {method}")

        breaker = self._breakers[method]
        limiter = self._limiters[method]
        max_retries = endpoint_cfg.max_retries
        base_timeout = timeout or endpoint_cfg.base_timeout

        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            # Rate limit
            await limiter.acquire()

            try:
                result = await breaker.call(self._do_post, method, params, base_timeout, files)
                return result
            except CircuitBreakerOpen as e:
                logger.warning("Circuit breaker OPEN for %s, retry after %.1fs", method, e.retry_after)
                await asyncio.sleep(e.retry_after)
                continue
            except aiohttp.ClientResponseError as e:
                last_error = e
                if e.status == 429 and endpoint_cfg.retry_on_429:
                    retry_after = int(e.headers.get("Retry-After", 2 ** attempt))
                    logger.warning("Telegram 429 for %s, retry after %ds", method, retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if e.status >= 500 and endpoint_cfg.retry_on_500:
                    # Server error, reintentar con backoff
                    await asyncio.sleep(min(2 ** attempt, 60))
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(min(2 ** attempt, 60))
                continue

        raise last_error or RuntimeError(f"Max retries exceeded for {method}")

    async def _do_post(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        files: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Ejecuta el POST real."""
        url = self._url(method)
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        if files:
            # multipart/form-data para sendDocument
            data = aiohttp.FormData()
            for k, v in params.items():
                data.add_field(k, str(v))
            for k, (filename, content, content_type) in files.items():
                data.add_field(k, content, filename=filename, content_type=content_type)
            async with self.session.post(url, data=data, timeout=client_timeout) as resp:
                return await self._handle_response(resp, method)
        else:
            async with self.session.post(url, json=params, timeout=client_timeout) as resp:
                return await self._handle_response(resp, method)

    async def _handle_response(self, resp: aiohttp.ClientResponse, method: str) -> dict[str, Any]:
        if resp.status >= 400:
            text = await resp.text()
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=resp.status, message=text, headers=resp.headers
            )
        data = await resp.json()
        if not data.get("ok"):
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history, status=resp.status,
                message=data.get("description", "Telegram API error"), headers=resp.headers
            )
        return data

    # Métodos de conveniencia
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
        reply_markup: Optional[dict] = None,
    ) -> dict[str, Any]:
        params = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        return await self.post("sendMessage", params)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": parse_mode,
        }
        return await self.post("editMessageText", params)

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        caption: str = "",
    ) -> dict[str, Any]:
        params = {"chat_id": chat_id, "caption": caption[:1024]}
        files = {"document": (filename, content, "text/plain")}
        return await self.post("sendDocument", params, files=files)

    async def get_updates(
        self,
        offset: int,
        timeout: int = 25,
        limit: int = 100,
    ) -> dict[str, Any]:
        params = {"offset": offset, "timeout": timeout, "limit": limit}
        return await self.post("getUpdates", params, timeout=timeout + 15)

    async def set_my_commands(self, commands: list[dict], scope: Optional[dict] = None) -> dict[str, Any]:
        params = {"commands": commands}
        if scope:
            params["scope"] = json.dumps(scope)
        return await self.post("setMyCommands", params)

    async def answer_callback_query(self, callback_query_id: str) -> dict[str, Any]:
        return await self.post("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        emoji: str,
    ) -> dict[str, Any]:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        }
        return await self.post("setMessageReaction", params)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> dict[str, Any]:
        return await self.post("sendChatAction", {"chat_id": chat_id, "action": action})

    def get_breaker_state(self, method: str) -> str:
        return self._breakers.get(method, CircuitBreaker(method)).state

    def reset_breakers(self):
        for b in self._breakers.values():
            b.reset()


# Singleton getter para AsyncTelegramClient
_telegram_client_singleton: Optional[AsyncTelegramClient] = None


def get_telegram_client(config: dict) -> AsyncTelegramClient:
    """Obtiene o crea el cliente Telegram singleton."""
    global _telegram_client_singleton
    if _telegram_client_singleton is None:
        token = config.get("token")
        if not token:
            raise RuntimeError("Token de Telegram no configurado")
        _telegram_client_singleton = AsyncTelegramClient(token)
    return _telegram_client_singleton