"""UpdateDispatcher (F2b): serial + dedup + owner — the contract shared
by polling and webhook. Deterministic: synchronization by condition
(drain/join), never by time."""
import asyncio
import unittest

import support  # sys.path of src (deterministic)
from zcode_bot.dispatcher import UpdateDispatcher

import asynkit


def _cfg():
    return {"owner_user_id": 9}


def _msg(uid, text="hello"):
    return {"update_id": uid, "message": {"text": text, "from": {"id": 9}}}


class TestDispatcher(unittest.IsolatedAsyncioTestCase):

    async def _run_with(self, dispatcher, coro):
        task = asyncio.ensure_future(dispatcher.run())
        try:
            await coro()
            await dispatcher.drain()
        finally:
            task.cancel()

    async def test_processes_one_at_a_time_in_order(self):
        order_log, overlaps = [], []

        def process(cfg, msg):
            if order_log and order_log[-1] == "inside":
                overlaps.append(msg["update_id"])
            order_log.append("inside")
            order_log.append(msg["update_id"])
            order_log.append("outside")

        d = UpdateDispatcher(process, lambda cfg, m: True)
        await self._run_with(d, lambda: asyncio.gather(
            *[d.submit(_cfg(), _msg(i)) for i in range(5)]))
        self.assertEqual(order_log[1::3], [0, 1, 2, 3, 4])  # arrival order
        self.assertEqual(overlaps, [])                     # never in parallel

    async def test_dedup_by_update_id(self):
        calls = []
        d = UpdateDispatcher(lambda cfg, m: calls.append(m["update_id"]),
                             lambda cfg, m: True)
        await self._run_with(d, lambda: asyncio.gather(
            d.submit(_cfg(), _msg(7)),
            d.submit(_cfg(), _msg(7))))  # webhook retry
        self.assertEqual(calls, [7])
        self.assertEqual(d.duplicates, 1)

    async def test_no_owner_rejected(self):
        calls = []
        d = UpdateDispatcher(lambda cfg, m: calls.append(1),
                             lambda cfg, m: False)  # fail-closed
        ok, reason = (None, None)

        async def coro():
            nonlocal ok, reason
            ok, reason = await d.submit(_cfg(), _msg(1))
        await self._run_with(d, coro)
        self.assertFalse(ok)
        self.assertEqual(reason, "no-owner")
        self.assertEqual(calls, [])
        self.assertEqual(d.rejected, 1)

    async def test_exception_releases_seen_for_retry(self):
        attempts = []

        def process(cfg, msg):
            attempts.append(msg["update_id"])
            if len(attempts) == 1:
                raise RuntimeError("boom transitorio")

        d = UpdateDispatcher(process, lambda cfg, m: True)
        task = asyncio.ensure_future(d.run())
        try:
            ok, _ = await d.submit(_cfg(), _msg(3))
            self.assertTrue(ok)
            await d.drain()
            # the first processing failed: the update_id is NOT marked seen
            self.assertFalse(d.already_seen(3))
            ok, _ = await d.submit(_cfg(), _msg(3))  # retry → reprocesses
            self.assertTrue(ok)
            await d.drain()
            self.assertEqual(attempts, [3, 3])
        finally:
            task.cancel()

    async def test_dedup_expires_by_ttl(self):
        calls = []
        d = UpdateDispatcher(lambda cfg, m: calls.append(1),
                             lambda cfg, m: True, seen_ttl=0.05)
        await self._run_with(d, lambda: d.submit(_cfg(), _msg(5)))
        await asynkit.wait_until(lambda: not d.already_seen(5),
                                 timeout=5, msg="the TTL should have expired")
        ok, reason = await d.submit(_cfg(), _msg(5))
        self.assertTrue(ok, reason)

    async def test_drain_waits_for_whole_batch(self):
        processed = []

        def process(cfg, msg):
            processed.append(msg["update_id"])

        d = UpdateDispatcher(process, lambda cfg, m: True)
        task = asyncio.ensure_future(d.run())
        try:
            for i in range(20):
                await d.submit(_cfg(), _msg(i))
            await d.drain()
            self.assertEqual(len(processed), 20)
            self.assertEqual(d.pending(), 0)
        finally:
            task.cancel()

    async def test_on_error_reports_and_dispatcher_continues(self):
        errors = []
        calls = []

        def process(cfg, msg):
            calls.append(msg["update_id"])
            if msg["update_id"] == 1:
                raise RuntimeError("only the first one")

        d = UpdateDispatcher(process, lambda cfg, m: True,
                             on_error=errors.append)
        await self._run_with(d, lambda: asyncio.gather(
            d.submit(_cfg(), _msg(1)), d.submit(_cfg(), _msg(2))))
        self.assertEqual(len(errors), 1)
        self.assertEqual(calls, [1, 2])  # 2 still got processed


if __name__ == "__main__":
    unittest.main()
