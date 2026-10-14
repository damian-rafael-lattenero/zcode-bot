"""Kit reactivo de la suite (ADR-009): sincronización por condición,
NUNCA por tiempo. Reglas de la casa:

- NINGÚN test duerme ni coordina con sleeps. El rendezvous es
  `wait_until(cond)`: gira cediendo el loop (sleep(0) es yield, no una
  espera temporizada) hasta que la condición se cumple o vence el
  timeout con AssertionError.
- Las llamadas bloqueantes del código bajo prueba corren en executor
  (`call`) y se esperan con await: el loop queda libre para el resto
  del pipeline.
- `Sig` puentea hilos del SUT → loop: set() es thread-safe.
- `Chan` es el canal acotado productor→consumidor (sobre asyncio.Queue,
  con join()/task_done() para pools que agotan su trabajo).

Todas componen como pipeline: cada paso hace await y devuelve, así el
flujo de un test se lee como una cadena de pasos y escala linealmente
con la cantidad de productores/consumidores (más corrutinas en gather,
cero cambios en el kit).
"""
import asyncio
import time
import unittest

TestCase = unittest.IsolatedAsyncioTestCase  # base única de toda la suite


async def call(fn, *a, **k):
    """Corre una función bloqueante en el executor y espera su resultado."""
    return await asyncio.to_thread(fn, *a, **k)


async def wait_until(pred, timeout=5.0, msg=""):
    """Espera reactiva por condición: sleep(0) cede el loop (los demás
    pasos e hilos del SUT avanzan); vencido el timeout → AssertionError."""
    fin = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > fin:
            raise AssertionError(msg or "timeout esperando condición")
        await asyncio.sleep(0)
    return True


class Sig:
    """Evento puente hilo→loop: set() lo llama un hilo del SUT (o el
    proceso bajo prueba vía archivo), wait() lo espera desde el loop."""

    def __init__(self):
        self._ev = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def set(self):
        self._loop.call_soon_threadsafe(self._ev.set)

    async def wait(self, timeout=5.0):
        await asyncio.wait_for(self._ev.wait(), timeout)


class Chan:
    """Canal productor→consumidor acotado. Pool típico: M consumidores
    hacen `while True: x = await get(); ...; task_done()` y el orquestador
    cierra con `await join()` y cancela."""

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
