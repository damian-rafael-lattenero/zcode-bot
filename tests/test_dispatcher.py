"""UpdateDispatcher (F2b): serial + dedup + owner — el contrato que
comparten polling y webhook. Deterministas: sincronización por
condición (drain/join), nunca por tiempo."""
import asyncio
import unittest

import support  # sys.path de src (determinista)
from zcode_bot.dispatcher import UpdateDispatcher

import asynkit


def _cfg():
    return {"owner_user_id": 9}


def _msg(uid, text="hola"):
    return {"update_id": uid, "message": {"text": text, "from": {"id": 9}}}


class TestDispatcher(unittest.IsolatedAsyncioTestCase):

    async def _run_with(self, dispatcher, coro):
        task = asyncio.ensure_future(dispatcher.run())
        try:
            await coro()
            await dispatcher.drain()
        finally:
            task.cancel()

    async def test_procesa_uno_por_vez_y_en_orden(self):
        vistos, solape = [], []

        def process(cfg, msg):
            if vistos and vistos[-1] == "dentro":
                solape.append(msg["update_id"])
            vistos.append("dentro")
            vistos.append(msg["update_id"])
            vistos.append("fuera")

        d = UpdateDispatcher(process, lambda cfg, m: True)
        await self._run_with(d, lambda: asyncio.gather(
            *[d.submit(_cfg(), _msg(i)) for i in range(5)]))
        self.assertEqual(vistos[1::3], [0, 1, 2, 3, 4])  # orden de llegada
        self.assertEqual(solape, [])                     # nunca en paralelo

    async def test_dedup_por_update_id(self):
        llamadas = []
        d = UpdateDispatcher(lambda cfg, m: llamadas.append(m["update_id"]),
                             lambda cfg, m: True)
        await self._run_with(d, lambda: asyncio.gather(
            d.submit(_cfg(), _msg(7)),
            d.submit(_cfg(), _msg(7))))  # reintento del webhook
        self.assertEqual(llamadas, [7])
        self.assertEqual(d.duplicados, 1)

    async def test_no_owner_rechazado(self):
        llamadas = []
        d = UpdateDispatcher(lambda cfg, m: llamadas.append(1),
                             lambda cfg, m: False)  # fail-closed
        ok, motivo = (None, None)

        async def coro():
            nonlocal ok, motivo
            ok, motivo = await d.submit(_cfg(), _msg(1))
        await self._run_with(d, coro)
        self.assertFalse(ok)
        self.assertEqual(motivo, "no-owner")
        self.assertEqual(llamadas, [])
        self.assertEqual(d.rechazados, 1)

    async def test_excepcion_libera_seen_para_reintento(self):
        intentos = []

        def process(cfg, msg):
            intentos.append(msg["update_id"])
            if len(intentos) == 1:
                raise RuntimeError("boom transitorio")

        d = UpdateDispatcher(process, lambda cfg, m: True)
        task = asyncio.ensure_future(d.run())
        try:
            ok, _ = await d.submit(_cfg(), _msg(3))
            self.assertTrue(ok)
            await d.drain()
            # el primer procesamiento falló: el update_id NO queda marcado
            self.assertFalse(d.already_seen(3))
            ok, _ = await d.submit(_cfg(), _msg(3))  # reintento → reprocesa
            self.assertTrue(ok)
            await d.drain()
            self.assertEqual(intentos, [3, 3])
        finally:
            task.cancel()

    async def test_dedup_vence_por_ttl(self):
        llamadas = []
        d = UpdateDispatcher(lambda cfg, m: llamadas.append(1),
                             lambda cfg, m: True, seen_ttl=0.05)
        await self._run_with(d, lambda: d.submit(_cfg(), _msg(5)))
        await asynkit.wait_until(lambda: not d.already_seen(5),
                                 timeout=5, msg="el TTL debió vencer")
        ok, motivo = await d.submit(_cfg(), _msg(5))
        self.assertTrue(ok, motivo)

    async def test_drain_espera_todo_el_batch(self):
        procesados = []

        def process(cfg, msg):
            procesados.append(msg["update_id"])

        d = UpdateDispatcher(process, lambda cfg, m: True)
        task = asyncio.ensure_future(d.run())
        try:
            for i in range(20):
                await d.submit(_cfg(), _msg(i))
            await d.drain()
            self.assertEqual(len(procesados), 20)
            self.assertEqual(d.pending(), 0)
        finally:
            task.cancel()

    async def test_on_error_reporta_y_el_dispatcher_sigue(self):
        errores = []
        llamadas = []

        def process(cfg, msg):
            llamadas.append(msg["update_id"])
            if msg["update_id"] == 1:
                raise RuntimeError("solo el primero")

        d = UpdateDispatcher(process, lambda cfg, m: True,
                             on_error=errores.append)
        await self._run_with(d, lambda: asyncio.gather(
            d.submit(_cfg(), _msg(1)), d.submit(_cfg(), _msg(2))))
        self.assertEqual(len(errores), 1)
        self.assertEqual(llamadas, [1, 2])  # el 2 se procesó igual


if __name__ == "__main__":
    unittest.main()
