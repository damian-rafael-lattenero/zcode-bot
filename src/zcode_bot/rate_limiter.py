"""Async Rate Limiter — Token Bucket para respetar límites de Telegram (~30 msg/s, burst 5)."""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimiterConfig:
    rate: float = 30.0      # tokens por segundo (sostenido)
    burst: int = 5          # tokens máximos en bucket (ráfaga)


class AsyncRateLimiter:
    """Token bucket thread-safe para asyncio."""

    def __init__(self, config: Optional[RateLimiterConfig] = None):
        self.config = config or RateLimiterConfig()
        self._tokens = float(self.config.burst)
        self._last_update = time.monotonic()
        self._cond = asyncio.Condition()

    async def acquire(self, n: int = 1) -> None:
        """Espera hasta tener n tokens disponibles."""
        if n <= 0:
            raise ValueError(f"Request {n} tokens must be positive")
        if n > self.config.burst:
            raise ValueError(f"Request {n} tokens exceeds burst capacity {self.config.burst}")
        async with self._cond:
            while self._tokens < n:
                self._refill()
                if self._tokens < n:
                    wait_time = (n - self._tokens) / self.config.rate
                    try:
                        await asyncio.wait_for(
                            self._cond.wait_for(lambda: self._tokens >= n),
                            timeout=wait_time + 0.1
                        )
                    except asyncio.TimeoutError:
                        continue
            self._tokens -= n

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self.config.burst, self._tokens + elapsed * self.config.rate)
        self._last_update = now

    def try_acquire(self, n: int = 1) -> bool:
        """Intenta adquirir tokens sin bloquear. Devuelve True si éxito."""
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self._tokens

    def reset(self):
        """Reset para tests."""
        self._tokens = float(self.config.burst)
        self._last_update = time.monotonic()