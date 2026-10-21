"""poller — ASYNC driver of the command loop (F2d).

IDENTICAL semantics to the sync loop it replaces: reload gate
(reload-now + nuclear), orphan check, one-time menu, outbox, tg-busy
gate, short long-poll (3 s) and offset confirmed POST-dispatch
(FIX 14/10 — at-least-once). What changes is the DRIVER: long-poll and
processing run in the executor (`to_thread`) on an asyncio loop — the
same process can host the webhook server (F3) and supervisor tasks
without stealing the poll's thread.

DIP: no gateway here — the composition root (gateway.main) injects the
dependencies; the loop is tested with fakes without touching the god
file. The per-update body is gateway.process_update_sync (seam frozen
in test_process_update.py).
"""
import asyncio


class AsyncPoller:
    """The LaunchAgent's loop on asyncio. Each dependency is a sync
    callable (the bot's state is sync/battle-tested); ALL of them run in
    the executor — none blocks the loop."""

    def __init__(self, load_config, configured, fetch, current_offset,
                 save_offset, process, busy_fresh, orphans_check,
                 register_menu, outbox_flush, reload_pending, clear_reload,
                 log, poll_timeout=3, on_start=None, audit_fn=None,
                 queue_drain=None):
        self._load_config = load_config
        self._configured = configured
        self._fetch = fetch              # (cfg, offset, timeout) → (off, batch, n)
        self._current_offset = current_offset
        self._save_offset = save_offset
        self._process = process          # (cfg, msg); serial: await one at a time
        self._busy_fresh = busy_fresh
        self._orphans_check = orphans_check
        self._register_menu = register_menu
        self._outbox_flush = outbox_flush
        self._reload_pending = reload_pending
        self._clear_reload = clear_reload
        self._log = log
        self._poll_timeout = poll_timeout
        self._on_start = on_start        # housekeeping (once at startup)
        self._audit = audit_fn          # F30: invariants (own throttle)
        self._queue_check = queue_drain   # F37a: queue_check (own throttle)
        self.cycles = 0                  # minimal observability

    async def run_forever(self):
        if self._on_start:
            await asyncio.to_thread(self._on_start)
        menu_ok = False
        offset = None
        polling = False  # fetch/process/save in flight (see fail-fast below)
        while True:
            try:
                if await asyncio.to_thread(self._reload_pending):
                    await asyncio.to_thread(self._clear_reload)
                    self._log("errors", "reload-now: exiting to reload "
                              "code (F16: in-flight turns are separate "
                              "processes — they keep running and report "
                              "on their own)")
                    return
                cfg = await asyncio.to_thread(self._load_config)
                await asyncio.to_thread(self._orphans_check, cfg)
                if self._audit:  # F30: invariants (internal throttle)
                    await asyncio.to_thread(self._audit, cfg)
                if self._configured(cfg) and not menu_ok:
                    menu_ok = await asyncio.to_thread(self._register_menu, cfg)
                if self._configured(cfg):
                    await asyncio.to_thread(self._outbox_flush, cfg)
                    if self._queue_check:  # F37a: wakes dormant queues
                        await asyncio.to_thread(self._queue_check, cfg)
                polling = True
                if self._configured(cfg) and \
                        not await asyncio.to_thread(self._busy_fresh):
                    # F22: SHORT long-poll — latency ≤3 s; hooks share
                    # getUpdates (ADR-001) and a long poll would leave
                    # them without a window (409).
                    offset = None
                    try:
                        offset, messages, _n = await asyncio.to_thread(
                            self._fetch, cfg,
                            await asyncio.to_thread(self._current_offset),
                            self._poll_timeout)
                        # FIX 14/10: do NOT confirm here — POST-dispatch (below)
                    except Exception as exc:  # noqa: BLE001
                        self._log("errors", f"getUpdates failed: {exc}")
                        messages = []
                        # No sleep: the long-poll timeout already did the wait
                    for msg in messages:
                        try:
                            await asyncio.to_thread(self._process, cfg, msg)
                        except Exception as exc:  # noqa: BLE001
                            # (audit 20/10) a poisoned message must NOT
                            # drag the rest of the batch: before, the
                            # exception jumped to the outer except,
                            # which confirmed the offset of the WHOLE
                            # batch and the queue (k+1..n) was lost
                            # without reprocessing. Log and move on:
                            # the next healthy one runs.
                            self._log("errors",
                                      f"process crashed (moving on): {exc}")
                    if offset is not None:
                        await asyncio.to_thread(self._save_offset, offset)
                polling = False
                self.cycles += 1
            except Exception as exc:  # noqa: BLE001
                self._log("errors", f"watch-commands: {exc}")
                if self.cycles == 0 and not polling:
                    # F35b — first-cycle fail-fast: a WIRING bug in the
                    # setup (reload/config/orphans/auditor/menu/outbox)
                    # turned into silent deafness was the 19/10
                    # RELOAD_FILE incident (the poller lived 5 minutes
                    # deaf and the reload that would recover it read
                    # the same broken attribute). Dying is better:
                    # main() leaves a full traceback and launchd renews
                    # with a fresh traceback on every attempt. Failures
                    # INSIDE the poll (fetch/process/save, polling=True)
                    # are deliberately excluded: a poisoned message must
                    # not crash-loop (the offset is confirmed
                    # post-dispatch, the message would re-enter forever).
                    raise
                try:
                    if offset is not None:
                        self._save_offset(offset)  # attempted batch
                except Exception:  # noqa: BLE001
                    pass


async def supervisor_loop(load_config, configured, orphans_check,
                          register_menu, outbox_flush, reload_pending,
                          clear_reload, log, audit_fn=None, cadence=3.0,
                          queue_drain=None):
    """Supervisor cycle WITHOUT polling (webhook mode, F3): reload gate,
    orphans, invariant auditor (F30), menu and outbox. The cadence is
    REPORTING monitoring (a legitimate category in ADR-009) — it never
    shadows the dispatch, which belongs exclusively to the serial
    dispatcher."""
    menu_ok = False
    while True:
        try:
            if reload_pending():
                clear_reload()
                log("errors", "reload-now: exiting to reload code "
                              "(F16: in-flight turns are separate "
                              "processes — they keep running and report "
                              "on their own)")
                return
            cfg = load_config()
            orphans_check(cfg)
            if audit_fn:
                audit_fn(cfg)
            if configured(cfg):
                if not menu_ok:
                    menu_ok = register_menu(cfg)
                outbox_flush(cfg)
                if queue_drain:  # F37a: parity with the polling loop
                    queue_drain(cfg)
        except Exception as exc:  # noqa: BLE001 - the supervisor must not die
            log("errors", f"supervisor: {exc}")
        await asyncio.sleep(cadence)
