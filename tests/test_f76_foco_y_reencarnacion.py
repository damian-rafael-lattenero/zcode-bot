"""F76 — the stream focus is PINNED and the filling cajita is REBORN.

Two morning incidents (27/10), one root each:
- Damian picked /task @diff_ask and a phone conversation silently
  overwrote the pointer — his Mac turns on diff_ask streamed NOTHING
  («estoy en los logs de diff_ask y nada, cero thinking cero cots»).  # noqa: spanish — Damian's report, quoted
  focus_sid pins the stream; only /task moves it.
- A deploy killed the watcher mid-fill: the cajita stayed «filling» in  # noqa: spanish — Damian's report, quoted
  the chat forever, orphaned («el thinking anterior no se compactó»).  # noqa: spanish — Damian's report, quoted (continuation)
  The live pair (message id + label) now travels in the cache and the
  respawn ADOPTS the message. And with the store streaming the part
  (k), the display runs every poll again — no frozen cajita between
  seals («antes funcionaba más fluido»).  # noqa: spanish — Damian's report, quoted (continuation)
"""
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import core as zc_core  # noqa: E402
from zcode_bot import reasoning  # noqa: E402

GATE = {"token": "T", "chat_id": 1, "only_active_task": True}
CFG = {"token": "T", "chat_id": 1}   # gate-free: the watcher tests
#                                  # (the gate has its own above)
T0 = int(time.time()) - 120
SID = "sess_f76a0c0c-c0f0-49cc-a221-af3268d2a8f0"
CAP = reasoning.COT_BOX_CAP


def _msg(role, *parts):
    return {"info": {"role": role}, "parts": list(parts)}


def _r(text):
    return {"type": "reasoning", "text": text}


class TestFocusSid(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_the_round_trip_preserves_the_focus(self):
        zc_core.set_active_bot_session({"sid": "a", "title": "A", "ws": "/w",
                                        "focus_sid": "b"})
        act = zc_core.active_bot_session()
        self.assertEqual(act.focus_sid, "b")
        self.assertEqual(act.to_dict()["focus_sid"], "b")

    def test_the_focus_wins_over_the_active_sid(self):
        # the morning incident: he picked diff_ask, a phone conversation
        # moved the active record — the pinned task keeps streaming
        zc_core.set_active_bot_session({"sid": "phone", "title": "P",
                                        "ws": "/w", "focus_sid": "mac"})
        self.assertTrue(zc_core._task_current_ok(GATE, "mac"))
        self.assertFalse(zc_core._task_current_ok(GATE, "phone"))
        self.assertFalse(zc_core._task_current_ok(GATE, "other"))

    def test_without_a_focus_it_follows_the_active_sid(self):
        zc_core.set_active_bot_session({"sid": "mac", "title": "M",
                                        "ws": "/w"})
        self.assertTrue(zc_core._task_current_ok(GATE, "mac"))
        self.assertFalse(zc_core._task_current_ok(GATE, "phone"))

    def test_a_worker_turn_does_not_steal_the_focus(self):
        zc_core.set_active_bot_session({"sid": "old", "title": "O",
                                        "ws": "/w", "focus_sid": "mac"})
        zc_core.set_worker_session("phone", {"title": "P", "ws": "/w",
                                             "mode": "yolo"})
        act = zc_core.active_bot_session()
        self.assertEqual(act.sid, "phone")          # commands follow it
        self.assertEqual(act.focus_sid, "mac")      # the stream does not
        self.assertTrue(zc_core._task_current_ok(CFG, "mac"))
        # and when the worker IS the focus, nothing changes
        zc_core.set_active_bot_session({"sid": "x", "title": "X", "ws": "/w",
                                        "focus_sid": "phone"})
        zc_core.set_worker_session("phone", {"title": "P", "ws": "/w"})
        self.assertEqual(zc_core.active_bot_session().focus_sid, "phone")

    def test_a_task_pick_pins_the_focus(self):
        from zcode_bot.commands import handlers_tasks as ht
        picks = []

        def fake_ctx(n):
            return [{"sid": "mac", "title": "mac task", "ws": "/w"}]
        support.patch(self, ht, "_ctx_tasks", fake_ctx)
        support.patch(self, ht, "active_bot_session",
                      lambda: zc_core.active_bot_session())
        support.patch(self, zc_core, "set_active_bot_session",
                      lambda d: picks.append(d))
        support.patch(self, ht, "set_active_bot_session",
                      lambda d: picks.append(d))
        support.patch(self, ht, "esc", lambda s: s)
        from zcode_bot import agent_live
        support.patch(self, agent_live, "stop_views", lambda: None)
        ht._task_select_sid("mac")
        self.assertEqual(picks[-1].get("focus_sid"), "mac")


class TestLiveSeed(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_the_filling_cajita_travels_in_the_cache(self):
        led = reasoning.BoxLedger()
        led.boxes, led.labels = ["x" * 10], ["CoT 1.1"]
        led.box_phs = [None]
        led.live_birth(556, "1.2")
        reasoning._persist_cache(SID, T0, "cv", led, live=True)
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual((d.get("live_ph"), d.get("live_label")),
                         (556, "1.2"))
        # the respawn reads it BEFORE the startup wipe
        self.assertEqual(reasoning._live_seed(SID, T0), (556, "1.2"))

    def test_a_done_cache_seeds_nothing(self):
        reasoning.save_cache(SID, T0, "cv", ["x"], ["CoT 1.1"],
                             live_ph=556, live_label="1.2", done=True)
        self.assertEqual(reasoning._live_seed(SID, T0), (None, ""))

    def test_the_respawn_adopts_the_orphan_message(self):
        """F76c — the deploy killed the watcher mid-fill: the respawn
        continues editing THE SAME message and the store's settle
        freezes it with its button (no orphaned «filling» line)."""
        self.sends, self.edits = [], []
        support.patch(self, reasoning.uhttp, "send",
                      lambda cfg, text, loud=None, markup=None:
                      (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((ph, text, markup)), True)[1])
        support.patch(self, reasoning.time, "sleep", lambda s: None)
        reasoning.save_cache(SID, T0, "cv", [], [], phs=[],
                             live_ph=556, live_label="1.2", live=True)
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")

        class _ProtoSettling:
            """The store settles the pass over two polls: 1.1 first
            (with 1.2 still filling), then the whole pass."""

            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 2:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    full = [_msg("user", {"type": "text", "text": "p"}),
                            _msg("assistant", _r("a" * (CAP + 500)),
                                 {"type": "text", "text": "end"})]
                    if self.n == 1:
                        full = full[:1] + [_msg("assistant", _r("a" * CAP),
                                                 {"type": "text",
                                                  "text": "growing"})]
                    return {"result": {"messages": full}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoSettling(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w", catchup=True)
        # the orphan message was EDITED (the filling continues on it)
        # — never re-sent — and it froze with ITS button at the seal
        self.assertTrue(any(ph == 556 for ph, _t, _m in self.edits),
                        self.edits)
        freezes = [(t, m) for ph, t, m in self.edits if m]
        self.assertTrue(any("<b>CoT 1.2</b>" in t for t, _m in freezes))
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["phs"], [None, 556])   # 1.2 adopted by the seal

    def test_the_k_path_display_runs_every_poll(self):
        """F76b — with the store streaming the in-progress part (k), the
        filling cajita used to freeze between seals (updates only on
        box-count changes); it now edits EVERY poll."""
        self.sends, self.edits = [], []
        support.patch(self, reasoning.uhttp, "send",
                      lambda cfg, text, loud=None, markup=None:
                      (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((ph, text, markup)), True)[1])
        support.patch(self, reasoning.time, "sleep", lambda s: None)
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")

        class _ProtoStuck:
            """The store NEVER changes across polls (same boxes, k
            always the same in-progress part) — yet the filling cajita
            must keep receiving edits."""

            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 3:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": [
                        _msg("user", {"type": "text", "text": "p"}),
                        # the TAIL is an in-progress reasoning part: k
                        # is set and never changes across polls
                        _msg("assistant", _r("a" * (CAP + 700)))]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoStuck(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        lives = [t for _p, t, _m in self.edits if "filling CoT 1.2" in t]
        # born once + at least one re-edit on a NO-CHANGE poll
        self.assertGreater(len(lives), 1, self.edits)


if __name__ == "__main__":
    unittest.main()
