"""AsyncPoller (F2d): el driver async del loop de comandos, con fakes
inyectados (DIP — sin red ni god file). Semánticas críticas congeladas:
offset POST-dispatch, el loop no muere ante errores sistémicos, gate
tg-busy, recarga limpia, sin config no polla. Deterministas: el corte
es por condición (reload tras N chequeos), nunca por tiempo."""
import asyncio
import unittest

import support
from zcode_bot.poller import AsyncPoller


def _cfg():
    return {"owner_user_id": 9, "chat_id": 1, "token": "T", "enabled": True}


class FakeDeps:
    """Deps del poller con contadores. reload_after = ciclos completos
    antes de disparar la recarga (corte determinista del loop)."""

    def __init__(self, batches=(), busy=False, explota_en=None,
                 configured=True, reload_after=1):
        self.cfg = _cfg()
        self.batches = list(batches)
        self.fetch_calls = 0
        self.procesados = []
        self.eventos = []          # orden global: ("disp"|"save", valor)
        self.saved = []
        self.errores = []
        self.busy = busy
        self.configured_ok = configured
        self.explota_en = explota_en
        self.reload_after = reload_after
        self.reload_checks = 0
        self.cleared = False
        self.auditorias = 0  # F30: cuántos pases de auditoría corrieron

    def load_config(self):
        return self.cfg

    def configured(self, cfg):
        return self.configured_ok

    def busy_fresh(self):
        return self.busy

    def orphans_check(self, cfg):
        pass

    def auditar(self, cfg):  # F30: pasa por el ciclo junto a orphans_check
        self.auditorias += 1

    def register_menu(self, cfg):
        return True

    def outbox_flush(self, cfg):
        pass

    def reload_pending(self):
        self.reload_checks += 1
        return self.reload_checks > self.reload_after

    def clear_reload(self):
        self.cleared = True

    def log(self, kind, msg):
        self.errores.append(msg)

    def current_offset(self):
        return 0

    def save_offset(self, o):
        self.saved.append(o)
        self.eventos.append(("save", o))

    def fetch(self, cfg, offset, timeout):
        self.fetch_calls += 1
        batch = (self.batches[self.fetch_calls - 1]
                 if self.fetch_calls <= len(self.batches) else [])
        return 100 + self.fetch_calls, batch, len(batch)

    def process(self, cfg, msg):
        if self.explota_en is not None and len(self.procesados) == self.explota_en:
            raise RuntimeError("boom sistémico")
        self.procesados.append(msg.get("text"))
        self.eventos.append(("disp", msg.get("text")))

    async def run(self):
        p = AsyncPoller(
            load_config=self.load_config, configured=self.configured,
            fetch=self.fetch, current_offset=self.current_offset,
            save_offset=self.save_offset, process=self.process,
            busy_fresh=self.busy_fresh, orphans_check=self.orphans_check,
            register_menu=self.register_menu, outbox_flush=self.outbox_flush,
            reload_pending=self.reload_pending, clear_reload=self.clear_reload,
            log=self.log, auditar=self.auditar)
        await asyncio.wait_for(p.run_forever(), timeout=5)
        return p


def _msg(uid, text, chat_id=1, from_id=9):
    return {"update_id": uid, "message_id": uid, "text": text,
            "chat": {"id": chat_id}, "from": {"id": from_id}}


class TestAsyncPoller(unittest.IsolatedAsyncioTestCase):

    async def test_offset_confirmado_despues_del_dispatch(self):
        d = FakeDeps(batches=[[_msg(9, "/status")]])
        await d.run()
        self.assertEqual(d.eventos,
                         [("disp", "/status"), ("save", 101)])

    async def test_batch_varios_procesa_serial_y_confirma_una_vez(self):
        d = FakeDeps(batches=[[_msg(1, "/a"), _msg(2, "/b"), _msg(3, "/c")]])
        p = await d.run()
        self.assertEqual(d.procesados, ["/a", "/b", "/c"])
        self.assertEqual(d.saved, [101])
        self.assertEqual(p.ciclos, 1)

    async def test_auditar_corre_en_cada_ciclo_junto_a_orphans(self):
        # F30: el auditor es una dep del ciclo como orphans_check — un
        # pase por ciclo completo (3 acá; el 4º ciclo corta por reload)
        d = FakeDeps(reload_after=3)
        p = await d.run()
        self.assertEqual(p.ciclos, 3)
        self.assertEqual(d.auditorias, 3)

    async def test_excepcion_sistemica_no_mata_el_loop_y_confirma(self):
        # el catch registra y el batch igual se confirma (mismo criterio
        # del loop síncrono: duplicar es mejor que perder)
        d = FakeDeps(batches=[[_msg(9, "/status")]], explota_en=0,
                     reload_after=1)
        await d.run()
        self.assertTrue(d.errores)          # el catch corrió
        self.assertEqual(d.saved, [101])    # batch intentado: no reprocesar eterno

    async def test_failfast_setup_roto_en_primer_ciclo(self):
        """F35b — lección ARCH_RELOAD (19/10): un bug de wiring en el
        SETUP (acá: orphans_check) del primer ciclo MATA el poller en
        vez de dejarlo sordo para siempre; cada restart de launchd deja
        traceback fresco. (El mismo bug a mitad de vuelo sí se traga.)"""
        class OrphansRoto(FakeDeps):
            def __init__(self, **k):
                super().__init__(**k)
                self.orphans_calls = 0

            def orphans_check(self, cfg):
                self.orphans_calls += 1
                raise AttributeError("no attribute 'ARCH_RELOAD'")

        d = OrphansRoto(reload_after=99)
        with self.assertRaises(AttributeError):
            await d.run()       # sin fail-fast: TimeoutError del wait_for
        self.assertTrue(d.errores)          # el error igual quedó logueado

    async def test_setup_roto_tras_ciclo_bueno_vuelve_al_nunca_morir(self):
        """F35b — el fail-fast es SOLO wiring del primer ciclo: con un
        ciclo completo previo (ciclos==1) la misma excepción de setup se
        traga y el loop sigue hasta el reload (transitorios no matan)."""
        class OrphansRotoTarde(FakeDeps):
            def __init__(self, **k):
                super().__init__(**k)
                self.orphans_calls = 0

            def orphans_check(self, cfg):
                self.orphans_calls += 1
                if self.orphans_calls >= 2:
                    raise RuntimeError("transitorio del ciclo 2")
        d = OrphansRotoTarde(reload_after=2)
        p = await d.run()
        self.assertEqual(p.ciclos, 1)       # el ciclo 1 completó y queda
        self.assertTrue(d.errores)          # el del ciclo 2 quedó logueado

    async def test_fetch_falla_no_confirma_offset(self):
        def fetch_explota(cfg, offset, timeout):
            d.fetch_calls += 1
            raise RuntimeError("red caída")
        d = FakeDeps(reload_after=2)
        p = AsyncPoller(load_config=d.load_config, configured=d.configured,
                        fetch=fetch_explota, current_offset=d.current_offset,
                        save_offset=d.save_offset, process=d.process,
                        busy_fresh=d.busy_fresh, orphans_check=d.orphans_check,
                        register_menu=d.register_menu,
                        outbox_flush=d.outbox_flush,
                        reload_pending=d.reload_pending,
                        clear_reload=d.clear_reload, log=d.log)
        await asyncio.wait_for(p.run_forever(), timeout=5)
        self.assertTrue(d.errores)
        self.assertEqual(d.saved, [])       # nada que confirmar

    async def test_busy_gate_saltea_el_poll(self):
        d = FakeDeps(busy=True, reload_after=2)
        await d.run()
        self.assertEqual(d.fetch_calls, 0)  # tg-busy: getUpdates NO es tocado
        self.assertEqual(d.procesados, [])

    async def test_sin_config_no_polla(self):
        d = FakeDeps(configured=False, reload_after=1)
        await d.run()
        self.assertEqual(d.fetch_calls, 0)

    async def test_recarga_sale_limpio_sin_poll(self):
        d = FakeDeps(reload_after=0)
        await d.run()
        self.assertTrue(d.cleared)
        self.assertEqual(d.fetch_calls, 0)

    async def test_grupo_genera_rastro_y_no_responde(self):
        # semántica F22 del loop (con el process REAL del gateway)
        zt = support.fresh(self)
        sends, pwlogs = [], []
        support.parchea(self, zt, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (sends.append(text), True)[1])
        support.parchea(self, zt, "pwlog",
                        lambda *a: pwlogs.append(" ".join(map(str, a))))
        support.parchea(self, zt, "react", lambda *a, **k: None)
        support.parchea(self, zt, "typing", lambda *a, **k: None)
        cfg = dict(zt.DEFAULTS, token="T", chat_id=1, owner_user_id=9,
                   enabled=True)
        d = FakeDeps(batches=[[_msg(2, "hola desde el grupo",
                                   chat_id=-100123)]])
        p = AsyncPoller(
            load_config=lambda: cfg, configured=d.configured,
            fetch=d.fetch, current_offset=d.current_offset,
            save_offset=lambda o: None, process=zt.process_update_sync,
            busy_fresh=d.busy_fresh, orphans_check=d.orphans_check,
            register_menu=d.register_menu, outbox_flush=d.outbox_flush,
            reload_pending=d.reload_pending, clear_reload=d.clear_reload,
            log=d.log)
        await asyncio.wait_for(p.run_forever(), timeout=5)
        self.assertEqual(sends, [])  # NO responde al grupo
        self.assertTrue(any("otro chat" in x for x in pwlogs))


if __name__ == "__main__":
    unittest.main()
