"""telegram — Telegram async transport (aiohttp).

client.py: AsyncTelegramClient (circuit breaker + per-endpoint rate
limiter + 429/500 retries). The single source of the three modules;
the flat http_client/rate_limiter/circuit_breaker are compatibility
shims for tests and webhook_server.
"""
from zcode_bot.telegram.circuit_breaker import (  # noqa: F401
    CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpen)
from zcode_bot.telegram.rate_limiter import (  # noqa: F401
    AsyncRateLimiter, RateLimiterConfig)
