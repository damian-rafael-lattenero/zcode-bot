"""Tests para AsyncRateLimiter."""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zcode_bot.rate_limiter import AsyncRateLimiter, RateLimiterConfig


class TestAsyncRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_burst_allows_immediate(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=10, burst=5))
        for _ in range(5):
            await rl.acquire(1)
        # 6to debe esperar
        start = time.monotonic()
        await rl.acquire(1)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.09)  # ~0.1s para 1 token a rate=10

    async def test_refill_over_time(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=100, burst=10))
        await rl.acquire(10)  # agota bucket
        self.assertLessEqual(rl.available_tokens, 0.1)

        await asyncio.sleep(0.05)  # 50ms -> ~5 tokens
        self.assertGreaterEqual(rl.available_tokens, 4)

    async def test_try_acquire_no_block(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=10, burst=3))
        self.assertTrue(rl.try_acquire(2))
        self.assertTrue(rl.try_acquire(1))
        self.assertFalse(rl.try_acquire(1))  # agotado

    async def test_acquire_multiple_tokens(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=50, burst=10))
        start = time.monotonic()
        await rl.acquire(5)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.05)  # inmediato, hay tokens

        start = time.monotonic()
        await rl.acquire(5)  # agota
        await rl.acquire(1)  # debe esperar ~0.02s
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.015)

    async def test_zero_tokens_invalid(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=10, burst=5))
        with self.assertRaises(ValueError):
            await rl.acquire(0)
        with self.assertRaises(ValueError):
            await rl.acquire(-1)

    async def test_exceeds_burst_invalid(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=10, burst=5))
        with self.assertRaises(ValueError):
            await rl.acquire(6)

    async def test_concurrent_acquires(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=1000, burst=10))
        results = []

        async def worker(n):
            await rl.acquire(n)
            results.append(n)

        await asyncio.gather(*[worker(1) for _ in range(10)])
        self.assertEqual(sum(results), 10)

    async def test_reset(self):
        rl = AsyncRateLimiter(RateLimiterConfig(rate=10, burst=5))
        await rl.acquire(5)
        self.assertLessEqual(rl.available_tokens, 0.1)
        rl.reset()
        self.assertEqual(rl.available_tokens, 5)


if __name__ == "__main__":
    unittest.main()