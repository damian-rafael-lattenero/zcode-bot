"""telegram — transporte async de Telegram (aiohttp).

client.py: AsyncTelegramClient (circuit breaker + rate limiter por
endpoint + retries 429/500). La fuente única de los tres módulos; los
planos http_client/rate_limiter/circuit_breaker son shims de
compatibilidad para tests y webhook_server.
"""
from zcode_bot.telegram.circuit_breaker import (  # noqa: F401
    CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpen)
from zcode_bot.telegram.rate_limiter import (  # noqa: F401
    AsyncRateLimiter, RateLimiterConfig)
