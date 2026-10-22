# -*- coding: utf-8 -*-
"""F58 — the ghost busy and the honest stop.

22/10 11:13-11:19: a turn killed by FORCE (/stop's kill -9 escalation)
never writes turn.completed/failed to the CLI log — which running_turns()
replays — so the session stayed «busy» for 2 h with nothing owning it:
every new message queued into a black hole, /stop answered «no longer
exists» (its only lever, the workers registry, was already empty), six
stops in 70 s, and a /nuclear. The tombstone (bot-forcestop.json) is the
record every force-death leaves behind, and /stop on a ghost clears it
and gives the queued messages back their runner."""
import time
import unittest
from unittest import mock

import support

from test_prompt_worker import _cfg, _FakeC, _QuickView

from zcode_bot import config as zc_config
from zcode_bot import core as zc_core
from zcode_bot import workers as zc_workers


def _forced(zt):
    return zt._store(zc_config.FORCE_STOP_FILE).read() or {}


class TestTombstone(unittest.IsolatedAsyncioTestCase):
    """running_turns() releases a session whose turn died by force — but
    only when the tombstone is NEWER than its turn.started (a real turn
    started after a force-stop is busy again, not masked)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1  # turn.started ~60 s ago, never ended

    def test_the_fixture_session_is_busy_before_any_tombstone(self):
        self.assertIn(self.T1, self.zt.running_turns())

    def test_a_force_stop_releases_the_busy_state(self):
        zc_core.force_turn_stopped(self.T1)
        self.assertNotIn(self.T1, self.zt.running_turns())
        self.assertIn(self.T1, _forced(self.zt))

    def test_an_older_tombstone_releases_nothing(self):
        zc_core.force_turn_stopped(self.T1, ts=time.time() - 120)
        # the fixture's turn.started is ~60 s old: NEWER than the tombstone
        self.assertIn(self.T1, self.zt.running_turns())

    def test_a_turn_started_after_the_tombstone_is_busy_again(self):
        evs = [(200.0, "sess_x", "turn.started", {})]

        class _St:
            def read(self):
                return {"sess_x": 150.0}  # force-stop BEFORE the start

        with mock.patch.object(zc_core, "zc_logs") as logs, \
                mock.patch.object(zc_core, "_store", lambda name: _St()):
            logs.iter_events.return_value = evs
            self.assertEqual(zc_core.running_turns(), {"sess_x": 200.0})


class TestStopOnAGhost(unittest.IsolatedAsyncioTestCase):
    """/stop against the ghost — the exact 11:17 sequence: busy in the
    CLI log, no worker, messages rotting in the queue."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1
        self.act = {"sid": self.T1, "title": "testing",
                    "ws": support.PROJ_BOT}

    def test_it_clears_the_ghost_and_relaunches_the_queue(self):
        self.zt._queue_put(self.T1, self.act, "the new direction")
        self.assertIn(self.T1, self.zt.running_turns())  # the ghost
        spawns = []
        with mock.patch.object(
                zc_workers, "_spawn_worker",
                side_effect=lambda cfg, act, text:
                (spawns.append(text), True)[1]):
            r = zc_workers.bot_stop(self.T1, cfg={"token": "T"})
        self.assertIn("ghost cleared", r)
        self.assertIn("relaunched", r)
        self.assertEqual(spawns, ["the new direction"])
        self.assertNotIn(self.T1, self.zt.running_turns())  # tombstoned
        self.assertEqual(self.zt._queue_peek(self.T1), [])   # consumed

    def test_only_the_first_is_relaunched_the_rest_requeue(self):
        for msg in ("first", "second"):
            self.zt._queue_put(self.T1, self.act, msg)
        spawns = []
        with mock.patch.object(
                zc_workers, "_spawn_worker",
                side_effect=lambda cfg, act, text:
                (spawns.append(text), True)[1]):
            zc_workers.bot_stop(self.T1, cfg={"token": "T"})
        self.assertEqual(spawns, ["first"])
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["second"])

    def test_without_cfg_the_messages_stay_queued_not_lost(self):
        self.zt._queue_put(self.T1, self.act, "one")
        r = zc_workers.bot_stop(self.T1)
        self.assertIn("ghost cleared", r)
        self.assertIn("stay queued", r)
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["one"])
        self.assertNotIn(self.T1, self.zt.running_turns())

    def test_a_free_session_without_run_still_says_so(self):
        # NOT busy in the CLI log and no worker: the classic answer stands
        self.assertIn("no longer exists",
                      zc_workers.bot_stop("sess_NADA"))


class TestEscalationTombstones(unittest.IsolatedAsyncioTestCase):
    """The kill -9 escalation is WHERE ghosts are born — now the
    tombstone is written at birth (22/10 11:13:31: the kill landed, the
    CLI end never did)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.S = support.T1
        self.kills = []

    def test_kill_9_writes_the_tombstone(self):
        from test_stop_brusco import _FakeReg
        reg = _FakeReg({self.S: {"t0": time.time(), "pid": 777,
                                 "title": "CV"}})
        # the escalation only fires for a LIVE turn (bot-active says so)
        self.zt._store("bot-active.json").write(
            {self.S: {"t0": time.time()}})
        for mod, name, val in (
                (zc_workers, "_workers", lambda: reg),
                (zc_workers, "_run_turn_ok", lambda pid: True),
                (zc_workers, "_proc_alive", lambda pid: True),
                (zc_workers, "_kill_runtime",
                 lambda pid: (self.kills.append(pid), True)[1]),
                (zc_workers.time, "sleep", lambda s: None),
        ):
            p = mock.patch.object(mod, name, val)
            p.start()
            self.addCleanup(p.stop)
        r = zc_workers.bot_stop(self.S, grace=0.0)
        self.assertIn("💀", r)
        self.assertEqual(self.kills, [777])
        self.assertIn(self.S, _forced(self.zt))
        self.assertNotIn(self.S, self.zt.running_turns())


class TestNuclearTombstones(unittest.IsolatedAsyncioTestCase):
    """Everything nuclear kills, dies without a CLI turn end — the
    tombstone covers phone runs AND Mac UI turns (the 22/10 black hole
    ended with a /nuclear that left no trace in the CLI log either)."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_every_stopped_run_is_tombstoned(self):
        from zcode_bot import nuclear as zc_nuclear
        fake_reg = type("R", (), {
            "items": staticmethod(lambda: []),
            "flag_stop": staticmethod(lambda s: None)})()
        with mock.patch.object(
                zc_nuclear, "_nuclear_snapshot",
                return_value=([("sess_w", {"t0": 1})], {"sess_m": 2}, 0)), \
            mock.patch.object(zc_nuclear, "_workers", lambda: fake_reg), \
            mock.patch.object(zc_nuclear, "_nuc_kill_own_runtimes",
                              lambda: None), \
            mock.patch.object(zc_nuclear, "app_restart_cycle",
                              lambda: {"app_ok": True, "notes": []}), \
            mock.patch.object(zc_nuclear, "_spawn_nuclear_watchdog",
                              lambda: None), \
            mock.patch.object(zc_nuclear, "_spawn_caffeinate",
                              lambda argv: None), \
            mock.patch.object(zc_nuclear.uhttp, "send",
                              lambda *a, **k: True), \
            mock.patch.object(zc_nuclear.time, "sleep",
                              lambda s: None), \
            mock.patch.object(zc_nuclear.subprocess, "run",
                              lambda *a, **k: None):
            zc_nuclear._nuclear_body({"token": "T"})
        forced = _forced(self.zt)
        self.assertIn("sess_w", forced)
        self.assertIn("sess_m", forced)


class TestNuclearHoldsTheMacAwake(unittest.IsolatedAsyncioTestCase):
    """F58b — the cycle must not race a re-sleep: the body detaches
    caffeinate for the whole cycle + recovery window."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_the_body_detaches_caffeinate_first(self):
        from zcode_bot import nuclear as zc_nuclear
        order = []
        fake_reg = type("R", (), {
            "items": staticmethod(lambda: []),
            "flag_stop": staticmethod(lambda s: None)})()

        with mock.patch.object(
                zc_nuclear, "_nuclear_snapshot",
                return_value=([], [], 0)), \
            mock.patch.object(zc_nuclear, "_workers", lambda: fake_reg), \
            mock.patch.object(zc_nuclear, "_nuc_kill_own_runtimes",
                              lambda: order.append("kill")), \
            mock.patch.object(zc_nuclear, "app_restart_cycle",
                              lambda: (order.append("restart") or
                                       {"app_ok": True, "notes": []})), \
            mock.patch.object(zc_nuclear, "_spawn_nuclear_watchdog",
                              lambda: None), \
            mock.patch.object(zc_nuclear, "_spawn_caffeinate",
                              lambda argv: order.append("caffeinate")), \
            mock.patch.object(zc_nuclear.uhttp, "send",
                              lambda *a, **k: True), \
            mock.patch.object(zc_nuclear.time, "sleep",
                              lambda s: None), \
            mock.patch.object(zc_nuclear.subprocess, "run",
                              lambda *a, **k: None):
            zc_nuclear._nuclear_body({"token": "T"})
        # caffeinate goes off BEFORE anything else (the cycle itself must
        # run on an awake machine)…
        self.assertEqual(order[0], "caffeinate")
        self.assertEqual(order[1], "kill")
        self.assertIn("restart", order)


class TestBusyNoticeButtons(unittest.IsolatedAsyncioTestCase):
    """F58 — the mid-turn message offers the interrupt when OUR worker
    owns the turn (phone run); a Mac-side turn keeps the plain notice
    (the phone cannot stop it)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1  # CLI-busy in the fixture
        self.act = {"sid": self.T1, "title": "testing",
                    "ws": support.PROJ_BOT}

    def _owning_worker(self):
        self.zt._workers().register(self.T1, {"t0": time.time(), "pid": 1,
                                              "title": "testing"})

    async def test_mid_turn_message_with_worker_gets_the_button(self):
        self._owning_worker()
        r = self.zt.handle_free_text({"token": "T"}, "the new direction")
        self.assertIsInstance(r, tuple)
        text, kb = r
        self.assertIn("queued", text)
        row = kb["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row],
                         ["⏹️ Interrupt · run this now", "⏳ Keep queued"])
        self.assertEqual(row[0]["callback_data"], "intr:%s:0" % self.T1)
        self.assertEqual(row[1]["callback_data"], "qkeep")
        # and the message IS in the queue, last (the button targets it)
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["the new direction"])

    async def test_mid_turn_message_on_the_mac_keeps_the_plain_notice(self):
        r = self.zt.handle_free_text({"token": "T"}, "hello")
        self.assertIsInstance(r, str)
        self.assertIn("Mac is busy", r)


class TestInterruptQueueItem(unittest.IsolatedAsyncioTestCase):
    """The ⏹️ primitive: front-run + cooperative markers, queue intact."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1

    def _owning_worker(self):
        self.zt._workers().register(self.T1, {"t0": time.time(), "pid": 1,
                                              "title": "testing"})

    async def test_it_front_runs_marks_and_keeps_the_rest(self):
        self._owning_worker()
        for m in ("older", "the redirect"):
            self.zt._queue_put(self.T1, {"sid": self.T1}, m)
        r = zc_workers._interrupt_queue_item({"token": "T"}, self.T1, 1)
        self.assertIn("Interrupting", r)
        self.assertIn("SAME runtime", r)
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["the redirect", "older"])   # front-run, kept
        self.assertTrue(zc_core._stop_requested(self.T1))
        self.assertTrue(zc_core._interrupt_pending(self.T1))

    async def test_on_a_mac_side_turn_it_only_front_runs(self):
        # no worker + CLI busy: nothing to stop — but the message is FIRST
        for m in ("older", "the redirect"):
            self.zt._queue_put(self.T1, {"sid": self.T1}, m)
        r = zc_workers._interrupt_queue_item({"token": "T"}, self.T1, 1)
        self.assertIn("Mac-side", r)
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["the redirect", "older"])
        self.assertFalse(zc_core._stop_requested(self.T1))
        self.assertFalse(zc_core._interrupt_pending(self.T1))

    async def test_a_gone_item_answers_honestly(self):
        self._owning_worker()
        self.zt._queue_put(self.T1, {"sid": self.T1}, "only")
        r = zc_workers._interrupt_queue_item({"token": "T"}, self.T1, 5)
        self.assertIn("no longer in the queue", r)
        self.assertFalse(zc_core._stop_requested(self.T1))

    async def test_qgo_on_an_owned_busy_session_stops_cooperatively(self):
        # F58-3: /queue's ⏫ shares the semantics — with OUR worker it is
        # the interrupt, not the old «still busy» refusal
        self._owning_worker()
        for m in ("older", "the redirect"):
            self.zt._queue_put(self.T1, {"sid": self.T1}, m)
        r = self.zt._cmd_qgo({"token": "T"}, "%s:1" % self.T1)
        self.assertIn("Interrupting", r)
        self.assertEqual([i["text"] for i in self.zt._queue_peek(self.T1)],
                         ["the redirect", "older"])
        self.assertTrue(zc_core._stop_requested(self.T1))
        self.assertTrue(zc_core._interrupt_pending(self.T1))


class TestWarmPickupPreamble(unittest.IsolatedAsyncioTestCase):
    """The whole redirect flow on the warm worker: the interrupt lands
    MID-TURN (the poll sets the markers), the cancelled turn reads ⏹️ not
    ❌, the pickup prepends the preamble, and the runtime is reused."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T2 = support.T2
        self.act = {"sid": self.T2, "title": "CV", "ws": support.PROJ_CV}
        self.sends = []
        self.deliveries = []
        self.c = _FakeC()
        self.makes = []
        from zcode_bot.utils import http as zc_http
        patches = [
            mock.patch.object(zc_http, "send",
                              lambda cfg, text, loud=None, markup=None:
                              (self.sends.append(text), True)[1]),
            mock.patch.object(zc_http, "_live_send", lambda cfg, text: 555),
            mock.patch.object(zc_http, "_live_edit",
                              lambda cfg, mid, text: True),
            mock.patch.object(zc_workers, "zc_proto",
                              type("M", (), {
                                  "make": staticmethod(
                                      lambda cwd=None, **kw:
                                      (self.makes.append(cwd), self.c)[1]),
                                  "proto_err": __import__(
                                      "zcode_bot.proto",
                                      fromlist=["proto_err"]).proto_err})),
            mock.patch.object(zc_workers, "LiveTurnView", _QuickView),
            mock.patch.object(zc_workers, "_send_long_reply",
                              lambda cfg, title, dur, text, max_parts=3:
                              self.deliveries.append((title, text))),
            mock.patch.object(zc_workers, "_WARM_STEP_S", 0.05),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_the_interrupted_pickup_runs_with_the_preamble(self):
        # scripted by TURN, not by call (the poll can fire twice per turn:
        # thread + direct fallback — both must agree). The FIRST turn's
        # poll sees exactly its own send out (n==1): the ⏹️ lands there;
        # from the second turn on (n==2), plain completions.
        def mid_turn_poll(sid, t0, **kw):
            n_sent = len([1 for m, _p in self.c.requests
                          if m == "session/send"])
            if n_sent <= 1:
                zc_core._interrupt_request(self.T2)
                zc_core._stop_request(self.T2)
                return {"event": "turn.failed",
                        "error": {"message": "Turn was cancelled"}}
            return {"event": "turn.completed"}

        with mock.patch.object(zc_workers, "_turn_end_poll", mid_turn_poll):
            zc_workers._queue_put(self.T2, self.act, "the redirect")
            zc_workers.prompt_worker(_cfg(self.zt, warm_keepalive_s=1),
                                     self.act, "first prompt")
        sent = [p for m, p in self.c.requests if m == "session/send"]
        self.assertEqual(len(sent), 2)
        # (the runtime-level notice may travel appended after the prompt)
        self.assertTrue(sent[0]["content"].startswith("first prompt"))
        self.assertTrue(sent[1]["content"].startswith(
            zc_workers._INTERRUPT_PREAMBLE))
        self.assertIn("the redirect", sent[1]["content"])
        # the flag was consumed: a LATER pickup runs without preamble
        self.assertFalse(zc_core._interrupt_pending(self.T2))
        # the interruption read ⏹️, not ❌ failed (and no retry button)
        self.assertTrue(any("interrupted" in s for s in self.sends))
        self.assertFalse(any("failed" in s for s in self.sends))
        # SAME runtime (warm): one make, closed only at the keepalive end
        self.assertEqual(len(self.makes), 1)
        self.assertTrue(self.c.closed)

    async def test_a_normal_failed_turn_still_reads_as_failed(self):
        # without the interrupt flag, the cancelled turn is the classic ❌
        with mock.patch.object(
                zc_workers, "_turn_end_poll",
                lambda *a, **k: {"event": "turn.failed",
                                 "error": {"message": "boom"}}):
            zc_workers.prompt_worker(_cfg(self.zt), self.act, "first")
        self.assertTrue(any("failed" in s for s in self.sends))


if __name__ == "__main__":
    unittest.main()