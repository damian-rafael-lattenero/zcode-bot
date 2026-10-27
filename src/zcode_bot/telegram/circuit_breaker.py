"""Circuit Breaker — protects external calls (Telegram API, ZCode endpoints).

States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (probing recovery).
Thread-safe for asyncio use (a single event loop).
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar, Awaitable, Tuple

T = TypeVar("T")


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5          # consecutive failures to open
    success_threshold: int = 2          # successes in HALF_OPEN to close
    timeout: float = 30.0               # seconds in OPEN before HALF_OPEN
    excluded_exceptions: tuple = ()     # exceptions that do NOT count as failures


class CircuitBreakerOpen(Exception):
    """Raised when the breaker is OPEN and rejects the call."""
    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"Circuit breaker '{name}' OPEN, retry after {retry_after:.1f}s")


class CircuitBreaker:
    """Asyncio-native implementation of the Circuit Breaker pattern."""

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
        """Runs func with circuit breaker protection."""
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
        except Exception:
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
        """Manual reset (for tests)."""
        self._state = "CLOSED"
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None

class SyncCircuitBreaker:
    """F79e — the same state machine for the SYNC transport (the
    polling path: http.py's send/fetch). When Telegram/network is down
    the bot must NOT hammer: the breaker opens, send() goes straight to
    the outbox (the poller flushes it once the breaker closes), and
    getUpdates — the cheapest call — becomes the natural HALF-OPEN
    probe. The healer reads state for the health line. time_fn is
    injectable (tests move the clock instead of sleeping)."""

    def __init__(self, name: str,
                 config: Optional[CircuitBreakerConfig] = None,
                 time_fn=None):
        import time as _time
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._time = time_fn or _time.monotonic
        self._state = "CLOSED"
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None

    @property
    def state(self) -> str:
        if self._state == "OPEN" and self._should_attempt_reset():
            return "HALF_OPEN"   # observational: the next call probes
        return self._state

    def call(self, func, *args, **kwargs):
        if self.state == "OPEN":
            waited = self._time() - (self._last_failure_time or 0.0)
            raise CircuitBreakerOpen(
                self.name, max(0.0, self.config.timeout - waited))
        if self._state == "OPEN":   # time moved: this call is the probe
            self._state = "HALF_OPEN"
            self._success_count = 0
        try:
            result = func(*args, **kwargs)
        except self.config.excluded_exceptions:
            raise
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _on_success(self):
        self._failure_count = 0
        if self._state == "HALF_OPEN":
            self._success_count += 1
            if self._success_count >= self.config.success_threshold:
                self._state = "CLOSED"
                self._success_count = 0

    def _on_failure(self):
        self._failure_count += 1
        self._last_failure_time = self._time()
        if self._state == "HALF_OPEN":
            self._state = "OPEN"
            self._success_count = 0
        elif self._state == "CLOSED" and \
                self._failure_count >= self.config.failure_threshold:
            self._state = "OPEN"

    def _should_attempt_reset(self) -> bool:
        if self._last_failure_time is None:
            return True
        return (self._time() - self._last_failure_time) >= self.config.timeout

    def reset(self):
        self._state = "CLOSED"
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
