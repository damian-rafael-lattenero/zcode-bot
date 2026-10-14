"""Circuit Breaker — protege llamadas externas (Telegram API, ZCode endpoints).
 
Estados: CLOSED (normal) → OPEN (fallando) → HALF_OPEN (probando recuperación).
Thread-safe para uso en asyncio (un solo event loop).
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar, Awaitable, Tuple

T = TypeVar("T")


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5          # fallos consecutivos para abrir
    success_threshold: int = 2          # éxitos en HALF_OPEN para cerrar
    timeout: float = 30.0               # segundos en OPEN antes de HALF_OPEN
    excluded_exceptions: tuple = ()     # excepciones que NO cuentan como fallo


class CircuitBreakerOpen(Exception):
    """Lanzado cuando el breaker está OPEN y rechaza la llamada."""
    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"Circuit breaker '{name}' OPEN, retry after {retry_after:.1f}s")


class CircuitBreaker:
    """Implementación asyncio-native del patrón Circuit Breaker."""

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = "CLOSED"          # CLOSED | OPEN | HALF_OPEN
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    async def call(self, func: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """Ejecuta func con protección del circuit breaker."""
        async with self._lock:
            if self._state == "OPEN":
                if self._should_attempt_reset():
                    self._state = "HALF_OPEN"
                    self._success_count = 0
                else:
                    retry_after = self.config.timeout - (time.monotonic() - self._last_failure_time)
                    raise CircuitBreakerOpen(self.name, max(0, retry_after))

        try:
            result = await func(*args, **kwargs)
        except self.config.excluded_exceptions:
            raise
        except Exception as e:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result

    async def _on_success(self):
        async with self._lock:
            self._failure_count = 0
            if self._state == "HALF_OPEN":
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._state = "CLOSED"
                    self._success_count = 0

    async def _on_failure(self):
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == "HALF_OPEN":
                self._state = "OPEN"
                self._success_count = 0
            elif self._state == "CLOSED" and self._failure_count >= self.config.failure_threshold:
                self._state = "OPEN"

    def _should_attempt_reset(self) -> bool:
        if self._last_failure_time is None:
            return True
        return (time.monotonic() - self._last_failure_time) >= self.config.timeout

    def reset(self):
        """Reset manual (para tests)."""
        self._state = "CLOSED"
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None