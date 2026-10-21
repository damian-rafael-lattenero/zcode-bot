"""AsyncPoller (F2d): the async driver of the command loop, with
injected fakes (DIP — no network, no god file). Frozen critical
semantics: offset POST-dispatch, the loop does not die on systemic
errors, tg-busy gate, clean reload, no polling without config.
Deterministic: the cut is by condition (reload after N checks), never
by time."""
import asyncio
import unittest

import support
from zcode_bot.poller import AsyncPoller


def _cfg():
    return {"owner_user_id": 9, "chat_id": 1, "token": "T", "enabled": True}


class FakeDeps:
    """Poller deps with counters. reload_after = full cycles before
    firing the reload (deterministic loop cut)."""

    def __init__(self, batches=(), busy=False, blows_up_at=None,
                 configured=True, reload_after=1):
        self.cfg = _cfg()
        self.batches = list(batches)
        self.fetch_calls = 0
        self.processed = []
        self.events = []           # global order: ("disp"|"save", value)
        self.saved = []
        self.errors = []
        self.busy = busy
        self.configured_ok = configured
        self.blows_up_at = blows_up_at
        self.reload_after = reload_after
        self.reload_checks = 0
        self.cleared = False
        self.audits = 0  # F30: how many audit passes ran

    def load_config(self):
        return self.cfg

    def configured(self, cfg):
        return self.configured_ok

    def busy_fresh(self):
        return self.busy

    def orphans_check(self, cfg):
        pass

    def run_audit(self, cfg):  # F30: goes through the cycle alongside orphans_check
        self.audits += 1

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
        self.errors.append(msg)

    def current_offset(self):
        return 0

    def save_offset(self, o):
        self.saved.append(o)
        self.events.append(("save", o))

    def fetch(self, cfg, offset, timeout):
        self.fetch_calls += 1
        batch = (self.batches[self.fetch_calls - 1]
                 if self.fetch_calls <= len(self.batches) else [])
        return 100 + self.fetch_calls, batch, len(batch)

    def process(self, cfg, msg):
        if self.blows_up_at is not None and len(self.processed) == self.blows_up_at:
            raise RuntimeError("systemic boom")
        self.processed.append(msg.get("text"))
        self.events.append(("disp", msg.get("text")))

    async def run(self):
        p = AsyncPoller(
            load_config=self.load_config, configured=self.configured,
            fetch=self.fetch, current_offset=self.current_offset,
            save_offset=self.save_offset, process=self.process,
            busy_fresh=self.busy_fresh, orphans_check=self.orphans_check,
            register_menu=self.register_menu, outbox_flush=self.outbox_flush,
            reload_pending=self.reload_pending, clear_reload=self.clear_reload,
            log=self.log, audit_fn=self.run_audit)
        await asyncio.wait_for(p.run_forever(), timeout=5)
        return p


def _msg(uid, text, chat_id=1, from_id=9):
    return {"update_id": uid, "message_id": uid, "text": text,
            "chat": {"id": chat_id}, "from": {"id": from_id}}


class TestAsyncPoller(unittest.IsolatedAsyncioTestCase):

    async def test_offset_confirmed_after_dispatch(self):
        d = FakeDeps(batches=[[_msg(9, "/status")]])
        await d.run()
        self.assertEqual(d.events,
                         [("disp", "/status"), ("save", 101)])

    async def test_batch_processes_serially_and_confirms_once(self):
        d = FakeDeps(batches=[[_msg(1, "/a"), _msg(2, "/b"), _msg(3, "/c")]])
        p = await d.run()
        self.assertEqual(d.processed, ["/a", "/b", "/c"])
        self.assertEqual(d.saved, [101])
        self.assertEqual(p.cycles, 1)

    async def test_run_audit_runs_each_cycle_alongside_orphans(self):
        # F30: the auditor is a cycle dep like orphans_check — one
        # pass per full cycle (3 here; the 4th cycle cuts on reload)
        d = FakeDeps(reload_after=3)
        p = await d.run()
        self.assertEqual(p.cycles, 3)
        self.assertEqual(d.audits, 3)

    async def test_systemic_exception_does_not_kill_the_loop_and_confirms(self):
        # the catch logs and the batch is still confirmed (same criterion
        # as the sync loop: duplicating beats losing)
        d = FakeDeps(batches=[[_msg(9, "/status")]], blows_up_at=0,
                     reload_after=1)
        await d.run()
        self.assertTrue(d.errors)           # the catch ran
        self.assertEqual(d.saved, [101])    # attempted batch: no eternal reprocessing

    async def test_failfast_broken_setup_on_first_cycle(self):
        """F35b — RELOAD_FILE lesson (19/10): a wiring bug in the SETUP
        (here: orphans_check) of the first cycle KILLS the poller
        instead of leaving it deaf forever; every launchd restart leaves
        a fresh traceback. (The same bug mid-flight IS swallowed.)"""
        class BrokenOrphans(FakeDeps):
            def __init__(self, **k):
                super().__init__(**k)
                self.orphans_calls = 0

            def orphans_check(self, cfg):
                self.orphans_calls += 1
                raise AttributeError("no attribute 'RELOAD_FILE'")

        d = BrokenOrphans(reload_after=99)
        with self.assertRaises(AttributeError):
            await d.run()       # without fail-fast: wait_for's TimeoutError
        self.assertTrue(d.errors)           # the error still got logged

    async def test_broken_setup_after_a_good_cycle_back_to_never_dying(self):
        """F35b — the fail-fast is ONLY about the first cycle's wiring:
        with one previous full cycle (cycles==1) the same setup
        exception is swallowed and the loop continues until the reload
        (transients don't kill)."""
        class BrokenOrphansLate(FakeDeps):
            def __init__(self, **k):
                super().__init__(**k)
                self.orphans_calls = 0

            def orphans_check(self, cfg):
                self.orphans_calls += 1
                if self.orphans_calls >= 2:
                    raise RuntimeError("transient of cycle 2")
        d = BrokenOrphansLate(reload_after=2)
        p = await d.run()
        self.assertEqual(p.cycles, 1)       # cycle 1 completed and stands
        self.assertTrue(d.errors)           # cycle 2's got logged

    async def test_fetch_failure_does_not_confirm_offset(self):
        def fetch_blows_up(cfg, offset, timeout):
            d.fetch_calls += 1
            raise RuntimeError("network down")
        d = FakeDeps(reload_after=2)
        p = AsyncPoller(load_config=d.load_config, configured=d.configured,
                        fetch=fetch_blows_up, current_offset=d.current_offset,
                        save_offset=d.save_offset, process=d.process,
                        busy_fresh=d.busy_fresh, orphans_check=d.orphans_check,
                        register_menu=d.register_menu,
                        outbox_flush=d.outbox_flush,
                        reload_pending=d.reload_pending,
                        clear_reload=d.clear_reload, log=d.log)
        await asyncio.wait_for(p.run_forever(), timeout=5)
        self.assertTrue(d.errors)
        self.assertEqual(d.saved, [])       # nothing to confirm

    async def test_busy_gate_skips_the_poll(self):
        d = FakeDeps(busy=True, reload_after=2)
        await d.run()
        self.assertEqual(d.fetch_calls, 0)  # tg-busy: getUpdates is NOT touched
        self.assertEqual(d.processed, [])

    async def test_without_config_no_polling(self):
        d = FakeDeps(configured=False, reload_after=1)
        await d.run()
        self.assertEqual(d.fetch_calls, 0)

    async def test_reload_exits_clean_without_polling(self):
        d = FakeDeps(reload_after=0)
        await d.run()
        self.assertTrue(d.cleared)
        self.assertEqual(d.fetch_calls, 0)

    async def test_group_logs_and_does_not_reply(self):
        # F22 semantics of the loop (with the gateway's REAL process)
        zt = support.fresh(self)
        sends, pwlogs = [], []
        support.patch(self, zt, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (sends.append(text), True)[1])
        support.patch(self, zt, "pwlog",
                        lambda *a: pwlogs.append(" ".join(map(str, a))))
        support.patch(self, zt, "react", lambda *a, **k: None)
        support.patch(self, zt, "typing", lambda *a, **k: None)
        cfg = dict(zt.DEFAULTS, token="T", chat_id=1, owner_user_id=9,
                   enabled=True)
        d = FakeDeps(batches=[[_msg(2, "hello from the group",
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
        self.assertEqual(sends, [])  # does NOT reply to the group
        self.assertTrue(any("another chat" in x for x in pwlogs))


if __name__ == "__main__":
    unittest.main()
