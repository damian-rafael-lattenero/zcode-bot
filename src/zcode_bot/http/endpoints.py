"""HTTP client endpoints configuration for Telegram Bot API."""
from dataclasses import dataclass, field
from typing import Any, Optional, Dict

from zcode_bot.circuit_breaker import CircuitBreakerConfig
from zcode_bot.rate_limiter import RateLimiterConfig


@dataclass
class TelegramEndpointConfig:
    """Configuración por endpoint de Telegram."""
    method: str
    rate_limit: RateLimiterConfig = field(default_factory=lambda: RateLimiterConfig(rate=30, burst=5))
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