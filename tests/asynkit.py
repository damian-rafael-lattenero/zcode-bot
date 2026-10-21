"""Reactive kit for the suite (ADR-009): synchronize on conditions,
NEVER on time. House rules:

- NO test sleeps or coordinates with sleeps. The rendezvous is
  `wait_until(cond)`: it spins yielding the loop (sleep(0) is a yield,
  not a timed wait) until the condition holds or the timeout expires
  with an AssertionError.
- Blocking calls from the code under test run in an executor
  (`call`) and are awaited: the loop stays free for the rest of the
  pipeline.
- `Sig` bridges SUT threads → loop: set() is thread-safe.
- `Chan` is the bounded producer→consumer channel (built on
  asyncio.Queue, with join()/task_done() for pools that drain their
  work).

Everything composes as a pipeline: each step awaits and returns, so a
test's flow reads as a chain of steps and scales linearly with the
number of producers/consumers (more coroutines in gather, zero
changes to the kit).
"""
import asyncio
import time
import unittest

TestCase = unittest.IsolatedAsyncioTestCase  # single base class for the whole suite


async def call(fn, *a, **k):
    """Runs a blocking function in the executor and awaits its result."""
    return await asyncio.to_thread(fn, *a, **k)


async def wait_until(pred, timeout=5.0, msg=""):
    """Reactive wait for a condition: sleep(0) yields the loop (other
    steps and SUT threads make progress); once the timeout expires
    → AssertionError."""
    fin = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > fin:
            raise AssertionError(msg or "timeout esperando condición")
        await asyncio.sleep(0)
    return True


class Sig:
    """Thread→loop bridge event: set() is called by an SUT thread (or
    the process under test via a file), wait() awaits it from the loop."""

    def __init__(self):
        self._ev = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def set(self):
        self._loop.call_soon_threadsafe(self._ev.set)

    async def wait(self, timeout=5.0):
        await asyncio.wait_for(self._ev.wait(), timeout)


class Chan:
    """Bounded producer→consumer channel. Typical pool: M consumers run
    `while True: x = await get(); ...; task_done()` and the orchestrator
    closes with `await join()` and cancels."""

    def __init__(self, maxsize=0):
        self._q = asyncio.Queue(maxsize)

    async def put(self, x):
        await self._q.put(x)

    async def get(self, timeout=None):
        if timeout is None:
            return await self._q.get()
        return await asyncio.wait_for(self._q.get(), timeout)

    def task_done(self):
        self._q.task_done()

    async def join(self):
        await self._q.join()
