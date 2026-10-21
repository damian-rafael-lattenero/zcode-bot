"""Tests for CircuitBreaker."""
import asyncio
import time
import unittest


from zcode_bot.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpen,
)


class TestCircuitBreaker(unittest.IsolatedAsyncioTestCase):
    async def test_closed_state_allows_calls(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))
        result = await cb.call(asyncio.sleep, 0)
        self.assertIsNone(result)
        self.assertEqual(cb.state, "CLOSED")

    async def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=2))

        async def fail():
            raise ValueError("boom")

        for _ in range(2):
            with self.assertRaises(ValueError):
                await cb.call(fail)

        self.assertEqual(cb.state, "OPEN")

    async def test_open_rejects_calls(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, timeout=10))

        async def fail():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            await cb.call(fail)  # 1 failure -> OPEN

        with self.assertRaises(CircuitBreakerOpen) as cm:
            await cb.call(asyncio.sleep, 0)
        self.assertEqual(cm.exception.name, "test")

    async def test_half_open_after_timeout(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, timeout=0.1))

        async def fail():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            await cb.call(fail)  # OPEN
        self.assertEqual(cb.state, "OPEN")

        await asyncio.sleep(0.15)  # timeout expired

        # next call -> HALF_OPEN
        result = await cb.call(asyncio.sleep, 0)
        self.assertIsNone(result)
        self.assertEqual(cb.state, "HALF_OPEN")

    async def test_half_open_closes_after_successes(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(
            failure_threshold=1, timeout=0.1, success_threshold=2
        ))

        async def fail():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            await cb.call(fail)  # OPEN
        await asyncio.sleep(0.15)  # -> HALF_OPEN

        # 2 successes -> CLOSED
        await cb.call(asyncio.sleep, 0)
        self.assertEqual(cb.state, "HALF_OPEN")
        await cb.call(asyncio.sleep, 0)
        self.assertEqual(cb.state, "CLOSED")

    async def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(
            failure_threshold=1, timeout=0.1, success_threshold=3
        ))

        async def fail():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            await cb.call(fail)  # OPEN
        await asyncio.sleep(0.15)  # -> HALF_OPEN

        await cb.call(asyncio.sleep, 0)  # 1 success
        with self.assertRaises(ValueError):
            await cb.call(fail)  # failure -> OPEN again
        self.assertEqual(cb.state, "OPEN")

    async def test_excluded_exceptions_dont_count(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(
            failure_threshold=2,
            excluded_exceptions=(KeyError,)
        ))

        async def fail_key():
            raise KeyError("excluded")

        async def fail_val():
            raise ValueError("counts")

        with self.assertRaises(KeyError):
            await cb.call(fail_key)
        with self.assertRaises(KeyError):
            await cb.call(fail_key)  # they don't count
        self.assertEqual(cb.state, "CLOSED")

        with self.assertRaises(ValueError):
            await cb.call(fail_val)  # it counts
        self.assertEqual(cb.state, "CLOSED")
        with self.assertRaises(ValueError):
            await cb.call(fail_val)  # 2nd -> OPEN
        self.assertEqual(cb.state, "OPEN")

    async def test_reset(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1))

        async def fail():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            await cb.call(fail)
        self.assertEqual(cb.state, "OPEN")

        cb.reset()
        self.assertEqual(cb.state, "CLOSED")

        await cb.call(asyncio.sleep, 0)  # works normally


if __name__ == "__main__":
    unittest.main()