# -*- coding: utf-8 -*-
"""F7++ — hard /stop.

session/stop is COOPERATIVE: the turn can keep running for seconds (a
tool in flight). bot_stop now escalates: if the worker doesn't die
within `grace` seconds → SIGKILL to the GROUP (worker + app-server:
start_new_session). /stop go = grace 0 (no waiting). Seams of the
owning module (workers): _workers / _proc_vivo / _run_turn_ok /
_kill_runtime.

(the real registry overwrites pid=os.getpid() of the registering
process — in production that's the worker; the fake here lets us pin
the pid for each case.)
"""
import unittest
from unittest import mock

import support

from zcode_bot import workers as zc_workers


class _FakeReg:
    def __init__(self, entries):
        self.d = dict(entries)

    def flag_stop(self, sid):
        e = self.d.get(sid)
        if e is None:
            return None
        e["stopped"] = True
        return e.get("titulo")

    def get(self, sid):
        e = self.d.get(sid)
        return dict(e) if e else None

    def items(self):
        return [(k, dict(v)) for k, v in
                sorted(self.d.items(), key=lambda kv: kv[1].get("t0") or 0)]

    def pop(self, sid):
        return self.d.pop(sid, None)


class TestStopBrusco(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.S = support.T1
        self.kills = []

    def _patches(self, reg, vivo=True, run_ok=True):
        ps = [
            mock.patch.object(zc_workers, "_workers", lambda: reg),
            mock.patch.object(zc_workers, "_run_turn_ok", lambda pid: run_ok),
            mock.patch.object(zc_workers, "_kill_runtime",
                              lambda pid: (self.kills.append(pid), True)[1]),
            mock.patch.object(zc_workers.time, "sleep", lambda s: None),
        ]
        if vivo is not None:
            ps.append(mock.patch.object(zc_workers, "_proc_vivo",
                                        lambda pid: vivo))
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def _reg(self, pid=4242):
        return _FakeReg({self.S: {"t0": 1, "titulo": "TT", "pid": pid}})

    async def test_muerto_a_tiempo_no_escalates(self):
        # session/stop stopped it in time: no kill, no 💀
        self._patches(self._reg(), vivo=False)
        r = zc_workers.bot_stop(self.S)
        self.assertTrue(r.startswith("🛑 Frenando"))
        self.assertNotIn("💀", r)
        self.assertEqual(self.kills, [])

    async def test_vivo_tras_grace_kill9_y_consume_marker(self):
        self._patches(self._reg(pid=4242), vivo=True)
        r = zc_workers.bot_stop(self.S, grace=0.2)
        self.assertIn("💀", r)
        self.assertEqual(self.kills, [4242])
        # the worker never reaches its finally: the marker is consumed by
        # bot_stop or the session's NEXT turn would abort on its own
        self.assertFalse(self.zt._stop_requested(self.S))

    async def test_pid_reciclado_no_se_toca(self):
        # ps doesn't confirm --run-turn: NEVER kill a pid that isn't ours
        self._patches(self._reg(pid=999), vivo=True, run_ok=False)
        r = zc_workers.bot_stop(self.S, grace=0.2)
        self.assertNotIn("💀", r)
        self.assertEqual(self.kills, [])

    async def test_grace_cero_mata_al_instante(self):
        # /stop go: no waiting for the cooperative stop
        self._patches(self._reg(pid=777), vivo=True)
        r = zc_workers.bot_stop(self.S, grace=0.0)
        self.assertIn("💀", r)
        self.assertEqual(self.kills, [777])

    async def test_sin_worker_solo_avisa(self):
        self._patches(_FakeReg({}), vivo=None)
        r = zc_workers.bot_stop(self.S)
        self.assertIn("ya no existe", r)
        self.assertEqual(self.kills, [])

    async def test_run_turn_ok_verifica_cmdline(self):
        # the real guardrail: only pids whose command is our worker
        from zcode_bot.workers import _run_turn_ok
        with mock.patch.object(
                zc_workers.subprocess, "run",
                lambda *a, **k: type("R", (), {"stdout":
                    "/usr/bin/python3 .../zcode-telegram.py --run-turn"})()):
            self.assertTrue(_run_turn_ok(1))
        with mock.patch.object(
                zc_workers.subprocess, "run",
                lambda *a, **k: type("R", (), {"stdout":
                    "/Applications/ZCode.app/Contents/MacOS/ZCode"})()):
            self.assertFalse(_run_turn_ok(2))


if __name__ == "__main__":
    unittest.main()
