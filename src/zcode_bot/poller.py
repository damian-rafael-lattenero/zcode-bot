"""poller — driver ASYNC del loop de comandos (F2d).

Semántica IDÉNTICA al loop síncrono que reemplaza: gate de recarga
(reload-now + nuclear), chequeo de huérfanos, menú una vez, outbox,
gate tg-busy, long-poll corto (3 s) y offset confirmado POST-dispatch
(FIX 14/10 — at-least-once). Lo que cambia es el DRIVER: long-poll y
procesamiento corren en el executor (`to_thread`) sobre un loop asyncio
— el mismo proceso puede hostear el webhook server (F3) y tareas
supervisoras sin robarle el hilo al poll.

DIP: acá no hay gateway — el composition root (gateway.main) inyecta
las dependencias; el loop se testea con fakes sin tocar el god file.
El cuerpo por-update es gateway.process_update_sync (seam congelado en
test_process_update.py).
"""
import asyncio


class AsyncPoller:
    """Loop del LaunchAgent sobre asyncio. Cada dependencia es un callable
    síncrono (el estado del bot es sync/battle-tested); TODAS corren en
    el executor — ninguna bloquea el loop."""

    def __init__(self, load_config, configured, fetch, current_offset,
                 save_offset, process, busy_fresh, orphans_check,
                 register_menu, outbox_flush, reload_pending, clear_reload,
                 log, poll_timeout=3, on_start=None, auditar=None):
        self._load_config = load_config
        self._configured = configured
        self._fetch = fetch              # (cfg, offset, timeout) → (off, batch, n)
        self._current_offset = current_offset
        self._save_offset = save_offset
        self._process = process          # (cfg, msg); serial: await uno por vez
        self._busy_fresh = busy_fresh
        self._orphans_check = orphans_check
        self._register_menu = register_menu
        self._outbox_flush = outbox_flush
        self._reload_pending = reload_pending
        self._clear_reload = clear_reload
        self._log = log
        self._poll_timeout = poll_timeout
        self._on_start = on_start        # housekeeping (una vez al arrancar)
        self._auditar = auditar          # F30: invariantes (throttle propio)
        self.ciclos = 0                  # observabilidad mínima

    async def run_forever(self):
        if self._on_start:
            await asyncio.to_thread(self._on_start)
        menu_ok = False
        offset = None
        pollando = False  # fetch/process/save en vuelo (ver fail-fast abajo)
        while True:
            try:
                if await asyncio.to_thread(self._reload_pending):
                    await asyncio.to_thread(self._clear_reload)
                    self._log("errors", "reload-now: saliendo para recargar "
                              "código (F16: los turnos en vuelo son procesos "
                              "separados — siguen y se reportan solos)")
                    return
                cfg = await asyncio.to_thread(self._load_config)
                await asyncio.to_thread(self._orphans_check, cfg)
                if self._auditar:  # F30: invariantes (throttle interno)
                    await asyncio.to_thread(self._auditar, cfg)
                if self._configured(cfg) and not menu_ok:
                    menu_ok = await asyncio.to_thread(self._register_menu, cfg)
                if self._configured(cfg):
                    await asyncio.to_thread(self._outbox_flush, cfg)
                pollando = True
                if self._configured(cfg) and \
                        not await asyncio.to_thread(self._busy_fresh):
                    # F22: long-poll CORTO — latencia ≤3 s; los hooks
                    # comparten getUpdates (ADR-001) y un poll largo los
                    # dejaría sin ventana (409).
                    offset = None
                    try:
                        offset, messages, _n = await asyncio.to_thread(
                            self._fetch, cfg,
                            await asyncio.to_thread(self._current_offset),
                            self._poll_timeout)
                        # FIX 14/10: NO confirmar acá — POST-dispatch (abajo)
                    except Exception as exc:  # noqa: BLE001
                        self._log("errors", f"getUpdates falló: {exc}")
                        messages = []
                        # Sin sleep: el long-poll timeout ya hizo el wait
                    for msg in messages:
                        await asyncio.to_thread(self._process, cfg, msg)
                    if offset is not None:
                        await asyncio.to_thread(self._save_offset, offset)
                pollando = False
                self.ciclos += 1
            except Exception as exc:  # noqa: BLE001
                self._log("errors", f"watch-commands: {exc}")
                if self.ciclos == 0 and not pollando:
                    # F35b — fail-fast del primer ciclo: un bug de WIRING
                    # en el setup (reload/config/huérfanos/auditor/menú/
                    # outbox) convertido en sordera silenciosa fue el
                    # incidente ARCH_RELOAD del 19/10 (el poller vivió 5
                    # minutos sordo y el reload que lo recuperaría leía
                    # el mismo atributo roto). Morir es mejor: main()
                    # deja traceback completo y launchd renueva con
                    # traceback fresco en cada intento. Fallas DENTRO del
                    # poll (fetch/process/save, pollando=True) quedan
                    # fuera a propósito: un mensaje envenenado no debe
                    # crash-loopear (el offset se confirma post-dispatch,
                    # el mensaje volvería a entrar eternamente).
                    raise
                try:
                    if offset is not None:
                        self._save_offset(offset)  # batch intentado
                except Exception:  # noqa: BLE001
                    pass


async def supervisor_loop(load_config, configured, orphans_check,
                          register_menu, outbox_flush, reload_pending,
                          clear_reload, log, auditar=None, cadence=3.0):
    """Ciclo de supervisor SIN polling (modo webhook, F3): gate de
    recarga, huérfanos, auditor de invariantes (F30), menú y outbox. La
    cadencia es monitoreo REPORTING (categoría legítima de ADR-009) —
    nunca tapa el dispatch, que es exclusivo del dispatcher serial."""
    menu_ok = False
    while True:
        try:
            if reload_pending():
                clear_reload()
                log("errors", "reload-now: saliendo para recargar código "
                              "(F16: los turnos en vuelo son procesos "
                              "separados — siguen y se reportan solos)")
                return
            cfg = load_config()
            orphans_check(cfg)
            if auditar:
                auditar(cfg)
            if configured(cfg):
                if not menu_ok:
                    menu_ok = register_menu(cfg)
                outbox_flush(cfg)
        except Exception as exc:  # noqa: BLE001 - el supervisor no muere
            log("errors", f"supervisor: {exc}")
        await asyncio.sleep(cadence)
