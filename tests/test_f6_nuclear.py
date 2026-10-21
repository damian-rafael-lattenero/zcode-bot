"""F6: /nuclear — emergency reset.

Freezes the contract: two-step confirmation (only 'go' executes — and
only the button produces 'go'), nuc:* callbacks, app restart cycle with
injected processes and clock, and presence in menu/help.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import asynkit
import support

from zcode_bot import nuclear as zc_nuclear          # owning module (F4e)
from zcode_bot.utils import http as zc_http          # transport seam


class FakeProc:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.returncode = 0


class _MultiPatch:
    """Several mock.patch.object as a single context manager."""

    def __init__(self, ctxs):
        self._ctxs = ctxs

    def __enter__(self):
        self._enters = [c.__enter__() for c in self._ctxs]
        return self

    def __exit__(self, *exc):
        for c in reversed(self._ctxs):
            c.__exit__(*exc)


class TestCallbacksNuclear(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_confirmation_buttons(self):
        self.assertEqual(self.zt._cb_text("nuc:go"), "/nuclear go")
        self.assertEqual(self.zt._cb_text("nuc:no"), "/nuclear no")


class TestNuclearCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_no_args_asks_confirmation_by_button(self):
        r = self.zt._cmd_nuclear({}, "")
        self.assertIsInstance(r, tuple)
        text, markup = r
        self.assertIn("Confirm?", text)
        cbs = {b["callback_data"] for b in markup["inline_keyboard"][0]}
        self.assertEqual(cbs, {"nuc:go", "nuc:no"})

    async def test_bare_nuclear_by_text_only_confirms(self):
        # typing "nuclear" (bare alias) shows the confirmation: NEVER executes
        r = self.zt.handle_command({}, "nuclear")
        self.assertIsInstance(r, tuple)
        self.assertIn("Confirm?", r[0])

    async def test_bare_alias_with_args_is_not_a_command(self):
        self.assertIsNone(self.zt.handle_command({}, "nuke fiesta"))

    async def test_no_cancels(self):
        self.assertIn("Cancelled", self.zt._cmd_nuclear({}, "no"))

    async def test_go_starts_nuclear_in_a_thread(self):
        # F4i: _cmd_nuclear lives in commands.dispatch — the stub goes there
        from zcode_bot.commands import handlers_system as zc_sistema
        ran = []
        pn = mock.patch.object(zc_sistema, "_nuclear_run",
                               lambda cfg: ran.append(cfg))
        pn.start()
        self.addCleanup(pn.stop)
        r = self.zt._cmd_nuclear({"x": 1}, "go")
        self.assertIn("Nuclear 2.0", r)
        # rendezvous by condition (the "go" thread fills the list):
        # no polling with sleeps
        await asynkit.wait_until(lambda: ran, timeout=5,
                                 msg="the 'go' should have launched _nuclear_run")
        self.assertEqual(ran, [{"x": 1}])


class TestAppRestartCycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    @staticmethod
    def _mkclock():
        clock = [0.0]

        def sleep(s):
            clock[0] += max(s, 1.0)

        return clock, sleep, (lambda: clock[0])

    async def test_happy_cycle(self):
        calls = []
        state = {"app": True, "host": False}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            return [999] if state["host"] else []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["had_app"])
        self.assertTrue(rep["app_ok"])
        self.assertEqual(rep["notes"], [])
        self.assertEqual([c[:2] for c in calls],
                         [["killall", "-TERM"], ["open", "-a"]])  # neither KILL nor pkill

    async def test_rebellious_app_sigkill_and_note(self):
        calls = []
        state = {"app": True, "host": False}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-KILL"]:
                state["app"] = False
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertIn("SIGKILL", rep["notes"][0])
        self.assertIn(["killall", "-KILL", "ZCode"], calls)

    async def test_orphan_host_terminated_with_pkill(self):
        calls = []
        state = {"app": True, "host": True}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["pkill", "-TERM"]:
                state["host"] = False
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            if "-P" in pgrep_args:
                return []
            return [999] if state["host"] else []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertTrue(any("orphan host" in n for n in rep["notes"]))
        self.assertIn(["pkill", "-TERM", "-f", "zcode-host-local"], calls)

    async def test_orphan_clis_closed_before_reopening(self):
        calls = []
        state = {"app": True, "cli": [555, 556]}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["kill", "-TERM"]:
                state["cli"] = [p for p in state["cli"] if p != int(args[2])]
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            if "-P" in pgrep_args:
                return list(state["cli"])
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertEqual(rep["notes"], [])
        self.assertIn(["kill", "-TERM", "555"], calls)
        self.assertIn(["kill", "-TERM", "556"], calls)
        # the reopening comes AFTER the cli deaths
        self.assertLess(calls.index(["kill", "-TERM", "556"]),
                        calls.index(["open", "-a", "ZCode"]))

    async def test_rebellious_cli_goes_to_sigkill_with_note(self):
        calls = []
        state = {"app": True, "cli": [555]}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["killall", "-TERM"]:
                state["app"] = False
            if args[:2] == ["kill", "-KILL"]:
                state["cli"] = []
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            if "-P" in pgrep_args:
                return list(state["cli"])
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertTrue(rep["app_ok"])
        self.assertTrue(any("SIGKILL" in n for n in rep["notes"]))
        self.assertIn(["kill", "-KILL", "555"], calls)

    async def test_without_app_only_opens_it(self):
        calls = []
        state = {"app": False, "host": False}
        clock, sleep, now = self._mkclock()

        def run(args, **kw):
            calls.append(args)
            if args[:2] == ["open", "-a"]:
                state["app"] = True
            return FakeProc()

        def pids(pgrep_args):
            if "-x" in pgrep_args:
                return [111] if state["app"] else []
            return []

        rep = self.zt.app_restart_cycle(run=run, pids=pids, sleep=sleep, now=now)
        self.assertFalse(rep["had_app"])
        self.assertTrue(rep["app_ok"])
        self.assertEqual([c[:2] for c in calls], [["open", "-a"]])


class TestKillOwnRuntimes(unittest.IsolatedAsyncioTestCase):
    """F16: the hard kill goes through the REGISTRY (worker-process pids),
    no longer through pgrep of the poller's children (the runtimes are
    grandchildren)."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_sigterm_to_the_registry_pids(self):
        import os as _os
        zt = self.zt
        zt._workers().register("s1", {"title": "a", "pid": _os.getpid()})
        zt._workers().register("s2", {"title": "b", "pid": _os.getpid()})
        # the own pid is excluded (there is never a worker of THIS process
        # in production, but the test uses getpid as a known pid)
        signals = []
        real_kill = _os.kill

        def fake_kill(pid, sig):
            if sig == 15:  # SIGTERM — not the SIGKILLs (9) of the kill
                signals.append(pid)  # phase: the pid is the test itself and it would die
            return real_kill(pid, sig) if sig != 9 else None

        with mock.patch.object(zt.os, "kill", fake_kill), \
                mock.patch.object(zt.time, "sleep"):
            zt._nuc_kill_own_runtimes()
        self.assertEqual(signals, [])  # own pids: excluded

    async def test_sigterm_to_foreign_pid(self):
        zt = self.zt
        signals = []
        # pid of a real ephemeral foreign process (kills nobody from the test)
        foreign = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            zt._workers().register("sX", {"title": "x", "pid": foreign.pid})
            foreign.kill()  # already dead: the test does not really touch it
            foreign.wait()
            # a dead pid: os.kill raises ProcessLookupError → ignored
            zt._nuc_kill_own_runtimes()
            signals.append("no-crash")
            self.assertEqual(signals, ["no-crash"])
        finally:
            try:
                foreign.kill()
                foreign.wait()
            except OSError:
                pass


class TestNuclearGuardReload(unittest.IsolatedAsyncioTestCase):
    async def test_flag_active_only_during_the_cycle(self):
        zt = support.fresh(self)
        # _nuclear_run SYNCHRONOUS with the body stubbed: the flag is
        # asserted INSIDE the cycle and purged right after — no threads
        # or coordination waits. F4e: the body is stubbed in the owning
        # module (nuclear.py), not in the gateway namespace
        seen = []

        def body(cfg):
            seen.append(zc_nuclear._nuclear_active())
            self.assertTrue(os.path.exists(zc_nuclear._nuclear_flag_path()))
        with mock.patch.object(zc_nuclear, "_nuclear_body", body):
            zt._nuclear_run({})
        self.assertEqual(seen, [True])
        self.assertFalse(zt._nuclear_active())
        self.assertFalse(os.path.exists(zt._nuclear_flag_path()))

    async def test_orphan_flag_with_ttl_gets_purged(self):
        # F21 — REGRESSION of incident 18:56: a nuclear kickstarts the
        # poller before its finally → orphaned flag → the reload gate
        # silently blocked ALL deploys for hours. TTL 120s: orphaned =
        # ignore AND clean up.
        zt = support.fresh(self)
        p = zt._nuclear_flag_path()
        with open(p, "w") as f:
            f.write(str(time.time() - 3600))  # 1 hour: orphaned
        old = time.time() - 3600
        os.utime(p, (old, old))
        self.assertFalse(zt._nuclear_active())  # does not block
        self.assertFalse(os.path.exists(p))     # and cleans itself up

    async def test_fresh_flag_blocks(self):
        zt = support.fresh(self)
        with open(zt._nuclear_flag_path(), "w") as f:
            f.write(str(time.time()))
        self.assertTrue(zt._nuclear_active())


class TestNuclear20(unittest.IsolatedAsyncioTestCase):
    """Nuclear 2.0 — an announced and verified operation.

    1 pre-flight announces N runs / M Mac turns / K queue kept
    2 lockdown: spawn/drain/resume blocked during the cycle
    3 halt WITHOUT clearing the queue (new semantics)
    4 heartbeat: the live flag is not purged for being old; without
      heartbeat, it is
    5 watchdog: single final report or failure notice; flag always clean
    """

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1
        self.sends = []

    # ---------- helpers ----------

    def _nuclear_ctx(self, **overrides):
        """Patches the OWNING MODULE (F4e): _nuclear_body/cmd_nuclear_watchdog
        live in nuclear.py and use their own bindings + uhttp.send."""
        ctxs = [mock.patch.object(
            zc_http, "send",
            lambda cfg, text, loud=None, markup=None:
                (self.sends.append(text), True)[1])]
        for k, v in overrides.items():
            ctxs.append(mock.patch.object(zc_nuclear, k, v))
        return _MultiPatch(ctxs)

    def _flag(self):
        return zc_nuclear._nuclear_flag_path()

    def _con_flag(self):
        self.zt._nuclear_heartbeat()

    def _fake_worker(self, sid, pid=424242):
        self.zt._workers().register(sid, {"pid": pid, "t0": time.time(),
                                       "title": "t", "prompt": "p",
                                       "ws": "/w"})

    # ---------- phase 0: pre-flight ----------

    async def test_preflight_announces_n_m_k(self):
        zt = self.zt
        self._fake_worker(self.T1)
        zt._queue_put(self.T1, {"sid": self.T1, "ws": "/w"}, "queue message")
        with self._nuclear_ctx(
                running_turns=lambda: {self.T1: time.time() - 10,
                                       "sess_otra": time.time() - 5},
                _nuc_kill_own_runtimes=lambda: None,
                app_restart_cycle=lambda: {"had_app": True, "app_ok": True,
                                           "notes": []},
                _spawn_nuclear_watchdog=lambda: None,
                subprocess=mock.Mock()):
            zc_nuclear._nuclear_body({})
        preflight = self.sends[0]
        # 1 bot worker (T1) + 1 Mac turn without a worker (sess_otra)
        self.assertIn("1 phone run", preflight)
        self.assertIn("1 Mac turn", preflight)
        self.assertIn("KEPT", preflight)
        self.assertIn("1 message", preflight)

    async def test_stop_keeps_the_queue(self):
        # regression: bot_stop used to clear the queue; the nuclear must NOT
        zt = self.zt
        self._fake_worker(self.T1)
        zt._queue_put(self.T1, {"sid": self.T1, "ws": "/w"}, "survived")
        with self._nuclear_ctx(
                _nuc_kill_own_runtimes=lambda: None,
                app_restart_cycle=lambda: {"had_app": False, "app_ok": True,
                                           "notes": []},
                _spawn_nuclear_watchdog=lambda: None,
                subprocess=mock.Mock()):
            zc_nuclear._nuclear_body({})
        self.assertEqual(len(zt._queue_peek(self.T1)), 1)
        self.assertTrue(zt._workers().stopped(self.T1))  # marker set
        self.assertTrue(os.path.exists(zt._stop_file(self.T1)))

    async def test_report_for_the_watchdog(self):
        zt = self.zt
        with self._nuclear_ctx(
                _nuc_kill_own_runtimes=lambda: None,
                app_restart_cycle=lambda: {"had_app": True, "app_ok": True,
                                           "notes": ["nota x"]},
                _spawn_nuclear_watchdog=lambda: None,
                subprocess=mock.Mock()):
            zc_nuclear._nuclear_body({})
        rep = zt._store("bot-nuclear-report.json").read()
        self.assertEqual(rep["stopped"], 0)
        self.assertEqual(rep["notes"], ["nota x"])
        self.assertTrue(rep["app_ok"])

    # ---------- phase 1: lockdown ----------

    async def test_spawn_blocked_during_nuclear(self):
        zt = self.zt
        self._con_flag()
        self.assertIsNone(zt._spawn_worker({}, {"sid": self.T1}, "hello"))
        zt._nuclear_active()  # purge so other tests are not dirtied

    async def test_drain_and_resume_asleep_during_nuclear(self):
        zt = self.zt
        self._con_flag()
        zt._queue_put(self.T1, {"sid": self.T1, "ws": "/w"}, "m")
        from zcode_bot import workers as zc_workers
        # FIX 15/10 (deploy incident): plain rebinds (`zc.X = mock`)
        # STAY ALIVE in the xdist worker and contaminate later tests in
        # the same process (the /task golden received the Mock here and
        # blew up with "x"[1] IndexError) — always patch.object
        with mock.patch.object(zc_workers, "running_turns", lambda: {}), \
             mock.patch.object(zc_workers, "_queue_launch_next",
                               mock.Mock(return_value="x")) as launch:
            zt._queue_drain({}, self.T1)
            zt._queue_resume({}, self.T1)
            launch.assert_not_called()
        zt._nuclear_active()  # purge

    # ---------- phase 3: heartbeat ----------

    async def test_heartbeat_keeps_the_flag_alive(self):
        zt = self.zt
        self._con_flag()
        self.assertTrue(zt._nuclear_active())  # heartbeated 0s ago
        zt._nuclear_heartbeat()
        self.assertTrue(zt._nuclear_active())
        os.remove(self._flag())

    async def test_without_heartbeat_gets_purged(self):
        zt = self.zt
        self._con_flag()
        old = time.time() - 3600
        os.utime(self._flag(), (old, old))  # 1 h without heartbeat
        # the FIRST check purges (nobody heartbeating = orphaned)
        self.assertFalse(zt._nuclear_active())
        self.assertFalse(os.path.exists(self._flag()))

    # ---------- phase 4: watchdog ----------

    def _watchdog_env(self, rep=None):
        # P6: this fixture keeps the LEGACY Spanish keys on purpose — a
        # report written by a pre-deploy body must still be read by the
        # new watchdog (from_dict is bilingual)
        zt = self.zt
        zt._store("bot-nuclear-report.json").write(rep or {
            "old_pid": 111111, "t0": time.time() - 40, "dur": 40,
            "frenadas": 2, "totales": 2, "mac": 1, "cola": 3,  # noqa: spanish — legacy report shape fixture (compat)
            "app_ok": True, "notes": []})
        return zt

    async def test_report_persists_new_keys_and_reads_both(self):
        # the NEW write is English-only; the OLD Spanish shape still reads
        rep = zc_nuclear.NuclearReport(old_pid=1, t0=2.0, dur=3, stopped=4,
                                       total=5, mac=6, queued=7, alive=8,
                                       app_ok=True, notes=["n"])
        self.assertEqual(rep.to_dict(), {
            "old_pid": 1, "t0": 2.0, "dur": 3, "stopped": 4, "total": 5,
            "mac": 6, "queued": 7, "alive": 8, "app_ok": True, "notes": ["n"]})
        legacy = {"old_pid": 1, "t0": 2.0, "dur": 3, "frenadas": 4,
                  "totales": 5, "mac": 6, "cola": 7, "vivos": 8,  # noqa: spanish — legacy report shape fixture (compat)
                  "app_ok": True, "notes": ["n"]}
        self.assertEqual(zc_nuclear.NuclearReport.from_dict(legacy), rep)
        self.assertEqual(
            zc_nuclear.NuclearReport.from_dict(rep.to_dict()), rep)

    async def test_watchdog_final_report_with_new_poller(self):
        zt = self._watchdog_env()
        with self._nuclear_ctx(
                load_config=lambda: {},
                subprocess=mock.Mock(**{"run.return_value":
                                        mock.Mock(stdout="%d\n" % os.getpid())})):
            zc_nuclear.cmd_nuclear_watchdog()
        final = self.sends[0]
        self.assertIn("complete and verified", final)
        self.assertIn("2/2", final)
        self.assertIn("Queue kept: 3", final)
        self.assertFalse(os.path.exists(self._flag()))  # cleanup ALWAYS
        self.assertFalse(os.path.exists(zt._nuclear_report_path()))

    async def test_watchdog_warns_if_the_poller_did_not_come_back(self):
        zt = self._watchdog_env()
        # injected clock: 1st call fixes the deadline, the following ones
        # are already expired → the while never even enters (no infinite loops)
        t0 = time.time()
        clock = mock.Mock()
        clock.time.side_effect = [t0] + [t0 + 200] * 50
        clock.sleep = lambda s: None
        with self._nuclear_ctx(
                load_config=lambda: {},
                subprocess=mock.Mock(**{"run.return_value":
                                        mock.Mock(stdout="")}),  # nothing alive
                time=clock):
            zc_nuclear.cmd_nuclear_watchdog()
        final = self.sends[0]
        self.assertIn("did NOT come back", final)
        self.assertFalse(os.path.exists(self._flag()))  # cleanup ALWAYS
        self.assertFalse(os.path.exists(zt._nuclear_report_path()))


class TestNuclearInMenuAndHelp(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_menu_includes_nuclear(self):
        cap = {}

        fake_tg_api = support.make_fake_tg_api(cap)

        from zcode_bot.utils import http as zc_http
        support.patch(self, zc_http, "tg_api", fake_tg_api)
        cfg = self.zt.load_config()
        cfg["chat_id"] = 4242
        self.assertTrue(self.zt.register_bot_menu(cfg))
        names = [c["command"] for c in cap["setMyCommands"]["commands"]]
        self.assertIn("nuclear", names)

    async def test_help_mentions_nuclear(self):
        self.assertIn("/nuclear", self.zt._cmd_help({}, ""))


if __name__ == "__main__":
    unittest.main()
