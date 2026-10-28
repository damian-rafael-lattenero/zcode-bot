"""ADR-019 — the agent-brain event bus publisher.

Invariants under test:
- off by default: no knob, no file, byte-for-byte the old behaviour;
- one JSON line per event with the contract fields; whole lines even when
  many threads append at once;
- a bus failure NEVER breaks a turn (write error counted, bug in publish
  swallowed by emit, the hook still writes its marker);
- one owner per turn: the hooks publish the Mac's turns only when no
  worker owns the session; the worker publishes its own phone turns with
  turn.started / tool.call / turn.completed under ONE turn id.
"""
import json
import os
import subprocess
import threading
import types
import unittest
from unittest import mock

import support
from test_e2e_turno import BATCHES_2PASSES, _E2EBase

from zcode_bot import bus  # noqa: E402
from zcode_bot import config as zc_config  # noqa: E402


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


def _lines():
    if not os.path.exists(zc_config.BUS_FILE):
        return []
    with open(zc_config.BUS_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines() if line]


class TestPublisher(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_off_by_default_writes_nothing(self):
        cfg = _cfg(self.zt)
        self.assertFalse(cfg["event_bus"])
        self.assertFalse(bus.publish(cfg, "turn.started", "ses_1", {"a": 1}))
        self.assertFalse(os.path.exists(zc_config.BUS_FILE))

    def test_publish_writes_the_contract_line(self):
        cfg = _cfg(self.zt, event_bus=True)
        self.assertTrue(bus.publish(cfg, "turn.started", "ses_1", {"title": "CV"},
                                    turn="1700000000000"))
        (ev,) = _lines()
        self.assertEqual(sorted(ev), ["id", "kind", "payload", "sid", "source", "ts", "turn"])
        self.assertEqual(len(ev["id"]), 32)
        self.assertTrue(ev["ts"].endswith("Z") and "T" in ev["ts"])
        self.assertEqual((ev["kind"], ev["sid"], ev["turn"], ev["source"]),
                         ("turn.started", "ses_1", "1700000000000", "bot"))
        self.assertEqual(ev["payload"], {"title": "CV"})

    def test_clip_bounds_free_text(self):
        self.assertEqual(bus.clip(None), "")
        self.assertEqual(bus.clip("abc"), "abc")
        clipped = bus.clip("x" * (bus.PAYLOAD_TEXT_CAP + 500))
        self.assertEqual(len(clipped), bus.PAYLOAD_TEXT_CAP + 1)
        self.assertTrue(clipped.endswith("…"))

    def test_turn_id_is_stable_for_one_t0(self):
        self.assertEqual(bus.turn_id(1761680000.123), "1761680000123")
        self.assertEqual(bus.turn_id(1761680000.123), bus.turn_id(1761680000.1234))

    def test_unknown_kind_is_a_programmer_error_that_emit_still_swallows(self):
        cfg = _cfg(self.zt, event_bus=True)
        with self.assertRaises(ValueError):
            bus.publish(cfg, "turn.exploded", "ses_1")
        before = bus.failures
        self.assertFalse(bus.tap(cfg, "turn.exploded", "ses_1"))
        self.assertEqual(bus.failures, before + 1)
        self.assertEqual(_lines(), [])

    def test_write_failure_is_counted_not_raised(self):
        cfg = _cfg(self.zt, event_bus=True)
        blocker = os.path.join(self.zt.config.BASE, "not-a-dir")
        with open(blocker, "w") as f:
            f.write("x")
        support.patch(self, zc_config, "BUS_FILE", os.path.join(blocker, "bus-events.jsonl"))
        before = bus.failures
        self.assertFalse(bus.publish(cfg, "turn.started", "ses_1"))
        self.assertFalse(bus.tap(cfg, "turn.started", "ses_1"))
        self.assertEqual(bus.failures, before + 2)

    def test_concurrent_appends_produce_whole_lines(self):
        cfg = _cfg(self.zt, event_bus=True)
        threads, per_thread = 32, 20

        def work(n):
            for i in range(per_thread):
                bus.publish(cfg, "tool.call", "ses_%d" % n, {"i": i, "pad": "p" * 1500})
        ts = [threading.Thread(target=work, args=(n,)) for n in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        lines = _lines()
        self.assertEqual(len(lines), threads * per_thread)
        self.assertEqual(len({ev["id"] for ev in lines}), threads * per_thread)


class TestHookTaps(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from zcode_bot import hooks as zc_hooks
        from zcode_bot.utils import http as zc_http
        self.zt = support.fresh(self)
        self.hooks = zc_hooks
        self.sends = []
        support.patch(self, zc_http, "send",
                      lambda cfg, text, loud=None, markup=None, a_outbox=True:
                      (self.sends.append(text), True)[1])
        support.patch(self, zc_hooks, "emit", lambda obj: None)
        support.patch(self, zc_hooks, "subprocess", types.SimpleNamespace(
            Popen=lambda *a, **k: None, DEVNULL=subprocess.DEVNULL))

    def _payload(self, sid, **kw):
        p = {"sessionId": sid, "cwd": "/fake/proj"}
        p.update(kw)
        return p

    async def test_mac_prompt_and_stop_publish_under_the_same_turn_id(self):
        cfg = _cfg(self.zt, event_bus=True, notify_prompt=False, notify_stop=False,
                   mac_cot_buttons=False, mac_spectator=False, agent_stream=False)
        sid = support.T1
        self.hooks.on_user_prompt(cfg, self._payload(sid, prompt="hello bus"))
        self.hooks.on_stop(cfg, self._payload(sid, responseText="the reply " * 3))
        evs = _lines()
        self.assertEqual([e["kind"] for e in evs], ["user.prompt", "turn.completed"])
        self.assertEqual({e["source"] for e in evs}, {"hook"})
        self.assertEqual({e["sid"] for e in evs}, {sid})
        self.assertTrue(evs[0]["turn"] and evs[0]["turn"] == evs[1]["turn"])
        self.assertEqual(evs[0]["payload"]["prompt"], "hello bus")
        self.assertEqual(evs[1]["payload"]["response"], "the reply " * 3)
        self.assertIsNotNone(evs[1]["payload"]["duration_s"])

    async def test_the_headless_notice_is_peeled_off_the_published_prompt(self):
        cfg = _cfg(self.zt, event_bus=True, notify_prompt=False,
                   mac_cot_buttons=False, mac_spectator=False, agent_stream=False)
        prompt = "real question\n\n—\n⚠️ (notice from the bot: you are running HEADLESS…)"
        self.hooks.on_user_prompt(cfg, self._payload(support.T1, prompt=prompt))
        (ev,) = _lines()
        self.assertEqual(ev["payload"]["prompt"], "real question")

    async def test_a_worker_owned_session_is_not_published_by_the_hooks(self):
        cfg = _cfg(self.zt, event_bus=True, notify_prompt=False, notify_stop=False)
        support.patch(self, self.hooks, "_worker_owned", lambda sid: True)
        self.hooks.on_user_prompt(cfg, self._payload(support.T1, prompt="x"))
        self.hooks.on_stop(cfg, self._payload(support.T1, responseText="y"))
        self.assertEqual(_lines(), [])

    async def test_golden_a_bus_failure_never_breaks_the_hook(self):
        cfg = _cfg(self.zt, event_bus=True, notify_prompt=False, notify_stop=False,
                   mac_cot_buttons=False, mac_spectator=False, agent_stream=False)

        def explode(*a, **k):
            raise RuntimeError("bug inside the bus")
        support.patch(self, bus, "publish", explode)
        before = bus.failures
        sid = support.T1
        self.hooks.on_user_prompt(cfg, self._payload(sid, prompt="still runs"))
        self.assertTrue(os.path.exists(zc_config.marker_path(sid)), "the marker is the turn")
        self.hooks.on_stop(cfg, self._payload(sid, responseText="done"))
        self.assertFalse(os.path.exists(zc_config.marker_path(sid)))
        self.assertEqual(bus.failures, before + 2)
        self.assertEqual(_lines(), [])


class TestWorkerTaps(_E2EBase):
    """The REAL prompt_worker + LiveTurnView on the fake runtime: the phone
    turn reaches the bus as started → tool.call → completed, one turn id."""

    def test_a_phone_turn_is_published_start_to_end_under_one_turn_id(self):
        self._run_turn(BATCHES_2PASSES, event_bus=True, cot_buttons=False)
        evs = _lines()
        kinds = [e["kind"] for e in evs]
        self.assertEqual(kinds[0], "turn.started")
        self.assertEqual(kinds[-1], "turn.completed")
        self.assertEqual(kinds.count("tool.call"),
                         sum(1 for batch in BATCHES_2PASSES for kind, _ in batch if kind == "tool_call"))
        self.assertEqual({e["sid"] for e in evs}, {support.T2})
        self.assertEqual({e["source"] for e in evs}, {"bot"})
        self.assertEqual(len({e["turn"] for e in evs}), 1, "every tap of one turn shares the id")
        self.assertEqual(evs[0]["payload"]["prompt"], "do something with several tools")
        self.assertEqual(evs[0]["payload"]["title"], "CV")
        self.assertEqual(evs[-1]["payload"]["reply"], "final reply of the turn")
        self.assertIn("cot_chars", evs[-1]["payload"])
        self.assertGreater(evs[-1]["payload"]["duration_s"], 0)

    def test_without_the_knob_the_same_turn_leaves_no_trace(self):
        self._run_turn(BATCHES_2PASSES, cot_buttons=False)
        self.assertEqual(_lines(), [])
