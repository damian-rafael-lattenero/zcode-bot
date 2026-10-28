"""F41/F43/F46/F57 — the Mac's CoT LIVE: size-based boxes with buttons.

The watchdog polls the history every 3s. F57: every box lives in its OWN
message — born «thinking… filling CoT R.C» at the box's START (text-only),
edited text-only while it fills and FROZEN at the seal («CoT R.C · size»
+ its single button, global index); a frozen message is never touched
again. There are no round messages anymore: the boxes are the timeline.
F75: the cajitas fill LIVE from the fork's live-io sidecar (the store
settles Mac passes in bulk and cannot), the THINKING message blinks in
its place for the no-CoT moments, and the collapsed landings are GONE —
what never showed live never travels (Damian, 27/10). Damian's model
(F43): a cap per box; F46: «CoT R.C» labels (reasoning block R, box C —
never two «CoT 1»). Base verified live 20/10: the reasoning part of the
step in progress is already readable in session/messages mid-step.
"""
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot.utils import http as zh  # noqa: E402

CFG = {"token": "T", "chat_id": 1}
# F51c: recent — the long-turn notice fires with an old t0 and the
# loop tests would blow up with an extra ⏱️ inside
# F51c: recent (120s ago: far from the real marker's second of
# turn 2 in the pickups, far from the notice threshold)
T0 = int(time.time()) - 120
SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
CAP = reasoning.COT_BOX_CAP


def _msg(role, *parts):
    return {"info": {"role": role}, "parts": list(parts)}


def _r(text):
    return {"type": "reasoning", "text": text}


class TestLiveBoxes(unittest.TestCase):
    def test_the_in_progress_box_does_not_travel_until_full(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("a" * (CAP + 700))),  # in progress: 1 full + remainder
        ]
        boxes, labels, pending, k = reasoning.live_boxes(msgs)
        self.assertEqual([len(c) for c in boxes], [CAP])
        self.assertEqual(labels, ["CoT 1.1"])
        self.assertEqual((pending, k), (700, "1.2"))

    def test_closed_passes_travel_whole(self):
        msgs = [
            _msg("user", {"type": "text", "text": "p"}),
            _msg("assistant", _r("a" * (CAP + 100)), {"type": "tool",
                                                       "tool": "Bash"}),
            _msg("assistant", _r("b" * 50), {"type": "text", "text": "end"}),
        ]
        boxes, labels, pending, k = reasoning.live_boxes(msgs)
        self.assertEqual([len(c) for c in boxes], [CAP, 100, 50])
        self.assertEqual(labels, ["CoT 1.1", "CoT 1.2", "CoT 2.1"])  # R.C
        self.assertEqual(pending, 0)


class _ProtoCreciente:
    """The history EVOLVES between polls (like the real turn):
    poll 1: one reasoning block in progress with 1 full box;
    poll 2: that block closed (remainder sealed) and another started;
    poll 3 (end): everything complete."""

    def __init__(self, marker):
        self.marker, self.seen, self.closed = marker, 0, False
        A = "a" * (CAP + 500)
        B = "b" * 300
        self.stages = [
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r(B))],
            [_msg("user", {"type": "text", "text": "p"}),
             _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
             _msg("assistant", _r(B), {"type": "text", "text": "end"})],
        ]

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            return {"result": {}}
        if method == "session/messages":
            stage = min(self.seen, len(self.stages) - 1)
            msgs = self.stages[stage]
            self.seen += 1
            if self.seen >= 3:  # F46: one poll sees the 2nd reasoning block closed
                try:
                    os.remove(self.marker)
                except OSError:
                    pass
            return {"result": {"messages": msgs}}
        return {"result": {}}

    def close(self):
        self.closed = True


class TestRunWatcher(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits = [], []
        support.patch(self, reasoning.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None, a_outbox=True:
                        (self.sends.append((text, markup)), 777)[1])
        support.patch(self, reasoning.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((text, markup)), True)[1])
        self.naps = []
        support.patch(self, reasoning.time, "sleep", lambda s: self.naps.append(s))
        with open(reasoning.marker_path(SID), "w") as f:
            f.write("{}")

    def test_long_turn_on_the_mac_blinks_instead_of_announcing(self):
        # F75 — the ⏱️ notice is gone with every other cartel; a stretched
        # silent turn keeps the THINKING message blinking (spinner +
        # clock re-edit it every poll) and closes with the plain ✅.
        class _ProtoSilent:
            """No events; on iteration k it consumes the marker (end)."""

            def __init__(self, marker, loops=3):
                self.marker, self.loops, self.n = marker, loops, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > self.loops:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages": []}}
                return {"result": {}}

            def close(self):
                pass

        proto = _ProtoSilent(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make",
                        lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        self.assertFalse([t for t, _m in self.sends if "has been running" in t])
        # the thinking message: born once, re-edited (the blink), closed ✅
        born = [t for t, _m in self.sends if "thinking…" in t]
        self.assertEqual(len(born), 1)
        self.assertIn("«cv»", born[0])
        self.assertTrue(any("turn closed" in t for t, _m in self.edits))

    def test_each_box_message_born_at_its_start_and_frozen_at_its_seal(self):
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        # F74's live line has its own test — keep this one on the F57 boxes
        support.patch(self, reasoning, "_live_line_text", lambda *a: "")
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        # F57: every box lives in its OWN message (fast turns close
        # before the 45 s pipe beat fires — its own test covers it).
        n0, mk0 = self.sends[0]
        self.assertIn("filling CoT 1.2 (500 chars)", n0)
        self.assertIsNone(mk0)                    # born WITHOUT keyboard
        # poll 2 seals 1.2: its message FREEZES with ITS single button
        # (global index 2)… and 2.1's own message is born below, text-only
        n21, _mk21 = next(s for s in self.sends
                          if "filling CoT 2.1 (300 chars)" in s[0])
        self.assertIsNone(_mk21)
        t12, k12 = self.edits[0]
        self.assertIn("<b>CoT 1.2</b>", t12)
        self.assertEqual([b["text"] for f in k12["inline_keyboard"] for b in f],
                         ["CoT 1.2"])
        self.assertEqual(k12["inline_keyboard"][0][0]["callback_data"].split(":")[-1],
                         "2")  # its OWN global index
        # 3) the close (F57): 2.1 — sealed in the last poll — freezes
        #    with ITS button. 1.1 — never born live — stays in the
        #    cache for /cot (F85a: NO closing bundle — cots land in
        #    the moment; the collapsed landing belongs to the
        #    task-switch re-attach alone).
        t21, k21 = self.edits[1]
        self.assertIn("<b>CoT 2.1</b>", t21)
        self.assertEqual([b["text"] for f in k21["inline_keyboard"] for b in f],
                         ["CoT 2.1"])
        self.assertFalse([t for t, m in self.sends
                          if m and "cots landed" in t])   # F85a: nothing lands
        self.assertEqual(len(self.edits), 2)   # nothing else is ever edited
        self.assertFalse(any("pass " in t for t, _m in self.edits))
        self.assertFalse(any("cut" in t for t, _m in self.edits))
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 1.2", "CoT 2.1"])
        self.assertEqual(d["phs"], [None, 777, 777])  # 1.1 never lived as
        #                                        a message — the collapsed
        #                                        bundle carries it
        self.assertTrue(d["done"])
        self.assertTrue(proto.closed)
        self.assertTrue(all(s == reasoning.LL_TICK_S for s in self.naps))
        # F84: the poll's sleep splits into 1s ticks (the ll blinks)
        # the tap on the 3rd box (2nd reasoning block) opens ITS text with ITS label
        out = reasoning.show(SID, T0, 3)
        self.assertIn("<pre>%s</pre>" % ("b" * 300), out)
        self.assertIn("<b>CoT 2.1</b>", out)

    def test_a_task_switch_mutes_the_turns_stream(self):
        """F71 — the incident (26/10): the user moved to the diff_ask
        task while the zcode_bot Mac turn kept running, and its boxes
        and beats kept landing over it. The F15 gate is read LIVE on
        every push now (before: spawn-time only); the boxes still seal
        in the cache (/cot of the task recovers them)."""
        from zcode_bot import core as zc_core
        zc_core.set_active_bot_session({"sid": "sess_other", "title": "o",
                                        "ws": "/w"})
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(dict(CFG, only_active_task=True), SID, "cv",
                              T0, "/w")
        self.assertEqual(self.sends, [])
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertTrue(d["done"])            # the turn closed, cached
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])

    def test_attach_mid_turn_resumes_at_once(self):
        """F72/F75 — the /task pick of a Mac turn that kept running muted
        (F71): the THINKING message and the filling cajita show life AT
        ONCE on the resume round. The gap's boxes never travel (no
        bundle — live-or-lost); the cache is whole for /cot.
        Damian, 26/10: «no comienza a streamear al instante»."""  # noqa: spanish — Damian's report, quoted
        ok = [True]
        support.patch(self, reasoning, "_task_current_ok",
                      lambda cfg, sid: ok[0])

        class _ProtoFlip(_ProtoCreciente):
            # the gate flips with the polls: live → switched away →
            # attached again (the third poll IS the /task pick, over the
            # SAME history as the muted one's — the filling box must be
            # born without a new seal); the turn closes one poll later
            def __init__(self, marker):
                super().__init__(marker)
                self.stages = [self.stages[0], self.stages[1], self.stages[1],
                               self.stages[2]]

            def request(self, method, params=None, timeout=None):
                if method != "session/messages":
                    return {"result": {}}
                stage = self.stages[min(self.seen, len(self.stages) - 1)]
                self.seen += 1
                ok[0] = self.seen != 1
                if self.seen >= 5:
                    try:
                        os.remove(self.marker)
                    except OSError:
                        pass
                return {"result": {"messages": stage}}

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoFlip(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(dict(CFG, only_active_task=True), SID, "cv",
                              T0, "/w")
        # the thinking message born on the resume round (it was muted
        # through the gap) and the filling cajita with it
        self.assertTrue(any("thinking…" in t for t, _mk in self.sends),
                        self.sends)
        self.assertTrue(any("filling CoT 2.1 (300 chars)" in t
                            for t, _mk in self.sends))
        # the gap's boxes land COLLAPSED on the resume round (F78c):
        # ONE message with their buttons — never silent loss, and
        # never a drip of catch-up boxes
        bundle = [s for s in self.sends if s[1] and "cots landed" in s[0]]
        self.assertEqual(len(bundle), 1)
        self.assertIn("1 cots landed", bundle[0][0])
        # the cache is whole: every box sealed, phs only for what lived
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])
        self.assertTrue(d["done"])

    def test_attach_respawn_inherits_messages_and_lands_the_rest_collapsed(self):
        """F72/F75/F78c — the attach respawns a DEAD cot watcher mid-turn
        (a deploy killed it): the predecessor's frozen messages are NOT
        re-sent (its cache seeds the phs) and the boxes the chat never
        saw land as ONE collapsed message at the respawn's first sync —
        the delivery guarantee, without a drip of catch-up boxes."""
        reasoning.save_cache(SID, T0, "cv", ["x" * CAP], ["CoT 1.1"],
                             phs=[555], perbox=True, live=True)

        class _ProtoFixed:
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
                    return {"result": {"messages": [
                        _msg("user", {"type": "text", "text": "p"}),
                        _msg("assistant", _r("a" * (CAP + 500)),
                             {"type": "text", "text": "end"})]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoFixed(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w", catchup=True)
        # 1.2 (never lived as a message) lands collapsed at the first
        # sync — with its button — and 1.1's frozen message is NOT
        # re-sent
        bundle = next((t, m) for t, m in self.sends
                      if m and "cots landed" in t)
        self.assertIn("CoT 1.2…CoT 1.2", bundle[0])
        self.assertFalse(any("CoT 1.1" in t and m for t, m in self.sends),
                        self.sends)
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["phs"], [555, None])  # inherited; 1.2 never lived
        self.assertTrue(d["done"])

    def test_attach_respawn_shows_life_at_once(self):
        """F72b/F75 — the attach respawn's FIRST round shows life AT
        ONCE: the thinking message is born before any sync material
        (the post-attach silence Damian saw, 26/10)."""
        class _ProtoThree:
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
                        _msg("assistant", _r("a" * (CAP + 500)),
                             {"type": "text", "text": "end"})]}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoThree(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w", catchup=True)
        texts = [t for t, _mk in self.sends]
        self.assertTrue(any("thinking…" in t for t in texts), texts)
        self.assertTrue(any("turn closed" in t for t, _m in self.edits))

    def test_re_attach_births_the_filling_box_without_a_new_seal(self):
        """F72c — the reading surface at re-attach: the display update
        only ran when a box SEALED, so the box in progress stayed mute
        after the /task pick until it filled — minutes of beat lines with
        nothing to read. The resume round births the filling box's live
        message AT ONCE (Damian, 26/10: «que empiece a crear la caja  # noqa: spanish — Damian's rule, quoted
        desde el momento en que ingrese a la sesión»)."""  # noqa: spanish — Damian's rule, quoted (continuation)
        ok = [True]
        support.patch(self, reasoning, "_task_current_ok",
                      lambda cfg, sid: ok[0])
        A = "a" * (CAP + 500)
        s1 = [_msg("user", {"type": "text", "text": "p"}),
              _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
              _msg("assistant", _r("b" * 300))]

        class _ProtoStaged:
            """Explicit stages per poll; the RESUME round (3rd) returns
            the SAME history as the muted one — no new seal, and the
            filling box must still be born."""

            def __init__(self, marker):
                self.marker, self.n = marker, 0
                self.stages = [
                    [_msg("user", {"type": "text", "text": "p"}),
                     _msg("assistant", _r(A))],
                    list(s1),
                    list(s1),   # the re-attach: nothing new sealed
                    [_msg("user", {"type": "text", "text": "p"}),
                     _msg("assistant", _r(A), {"type": "tool", "tool": "Bash"}),
                     _msg("assistant", _r("b" * 300),
                          {"type": "text", "text": "end"})],
                ]

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    msgs = self.stages[min(self.n, len(self.stages) - 1)]
                    self.n += 1
                    if self.n > len(self.stages):
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    ok[0] = self.n != 1   # muted during poll 2 only
                    return {"result": {"messages": msgs}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoStaged(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(dict(CFG, only_active_task=True), SID, "cv",
                              T0, "/w")
        # the gap: 1.1 (never lived) lands COLLAPSED on the resume
        # round (F78c) — 1.2 kept the live ph from before the mute
        bundle = next((t, m) for t, m in self.sends
                      if m and "cots landed" in t)
        self.assertIn("CoT 1.1…CoT 1.1", bundle[0])
        self.assertEqual([b["text"] for f in bundle[1]["inline_keyboard"]
                          for b in f], ["CoT 1.1"])
        # the reading surface: the filling box born on the resume round —
        # a round whose history was IDENTICAL to the muted one's
        self.assertTrue(any("filling CoT 2.1 (300 chars)" in t
                            for t, _mk in self.sends))
        # and the flow continues as always: it seals, its message freezes
        # with ITS button
        freezes = [(t, k) for t, k in self.edits if k]
        self.assertTrue(any("<b>CoT 2.1</b>" in t for t, _k in freezes))
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])
        self.assertEqual(d["phs"], [None, 777, 777])
        self.assertTrue(d["done"])

    def test_the_sidecar_feeds_the_live_preview(self):
        """F73/F75 — the fork's live-io sidecar: the filling box's
        message carries a capped PREVIEW of the reasoning — the Mac-side
        CoT can be READ live while it fills (the store path streams it
        when it shows the in-progress part)."""
        import datetime
        from zcode_bot import live_io
        with open(live_io.sidecar_path(SID), "w") as f:
            f.write(json.dumps({
                "type": "live_io_partial", "sessionId": SID,
                "reasoningChars": 4200, "textChars": 0,
                "reasoningTail": "the invariant holds if <cache> is atomic",
                "updatedAt": datetime.datetime.fromtimestamp(
                    time.time(), tz=datetime.timezone.utc).isoformat()}) + "\n")
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        # the filling box's live message carries the reasoning itself
        # (escaped: <cache> must not break Telegram's HTML)
        birth = next(t for t, _mk in self.sends if "filling CoT 1.2" in t)
        self.assertIn("<pre>", birth)
        self.assertIn("the invariant holds if &lt;cache&gt; is atomic", birth)

    def test_bulk_settling_without_a_sidecar_lands_collapsed_at_close(self):
        """F73b→F75→F78c→F85a — the bulk-settling store: passes land
        WHOLE (no in-progress part, k empty every poll). Without the
        fork's sidecar there is nothing to fill live from — the boxes
        live in the cache for /cot. F85a (28/10): NO closing bundle —  # noqa: spanish — Damian's rule, quoted
        «los cot llegan en el momento, no se acumulan»; the collapsed  # noqa: spanish — Damian's rule, quoted (continuation)
        landing is the task-switch re-attach's alone."""
        class _ProtoBulk:
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
                    stage1 = [
                        _msg("user", {"type": "text", "text": "p"}),
                        _msg("assistant", _r("a" * (CAP * 2)),
                             {"type": "text", "text": "end"})]
                    stage2 = stage1 + [
                        _msg("assistant", _r("b" * (CAP * 2)),
                             {"type": "text", "text": "end"})]
                    return {"result": {"messages":
                                       stage2 if self.n >= 2 else stage1}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoBulk(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        # F85a: nothing lands at the close — no collapsed bundle, no
        # catch-up drip; the cache carries the four unseen boxes
        self.assertFalse([t for t, m in self.sends
                          if m and "cots landed" in t])
        # the thinking message is the only other surface, and it closes
        self.assertTrue(any("thinking…" in t for t, _mk in self.sends))
        self.assertTrue(any("turn closed" in t for t, _m in self.edits))
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(len(d["boxes"]), 4)
        self.assertEqual(d["phs"], [None] * 4)   # they never lived as
        #                                      messages — the bundle is
        #                                      their collapsed carrier
        self.assertTrue(d["done"])

    def test_the_cajitas_fill_live_from_the_sidecar_and_seal_at_3k(self):
        """F75 — Damian, 27/10: «quiero ver el cot en vivo llenándose en  # noqa: spanish — Damian's request, quoted
        vivo los bytes y luego que muestre una caja con un botón». On a  # noqa: spanish — Damian's request, quoted (continuation)
        bulk-settling Mac turn (k always empty — the store cannot fill
        anything live) the fork's sidecar drives the cajitas: born at
        the box's start with the readable tail, edited as they grow,
        sealed the same poll they cross the 3k grid — button and all —
        and the next one born below. The pass ordinal comes from the
        log's truth, so the store's bulk settle AGREES on the labels and
        sync dedupes (no doubles)."""
        from zcode_bot import live_io as lio
        support.patch(self, reasoning, "_pass_count", lambda sid, t0: 1)
        A = "a" * CAP + "b" * 500
        stages = [
            {"reasoningChars": 100, "textChars": 0,
             "reasoningTail": "a" * 100, "textTail": ""},
            {"reasoningChars": CAP + 50, "textChars": 0,
             "reasoningTail": A[-CAP:], "textTail": ""},
            {"reasoningChars": CAP + 400, "textChars": 0,
             "reasoningTail": A[-CAP:], "textTail": ""},
            None,   # the request completed: the tail box seals
        ]
        calls = [0]

        def fake_latest(sid, max_age=900.0, read=None):
            i = min(calls[0], len(stages) - 1)
            calls[0] += 1
            return stages[i]
        support.patch(self, lio, "latest", fake_latest)

        class _ProtoBulk:
            """The whole pass settles only at the END (bulk) — k empty
            every poll; the cajitas' only source is the sidecar."""
            def __init__(self, marker):
                self.marker, self.n = marker, 0
                self.bare = [_msg("user", {"type": "text", "text": "p"})]
                self.full = self.bare + [
                    _msg("assistant", _r(A),
                         {"type": "text", "text": "end"})]

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 5:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages":
                                       self.full if self.n >= 4 else self.bare}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoBulk(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        texts = [t for t, _mk in self.sends]
        # born filling, with the readable tail
        self.assertTrue(any("filling CoT 1.1 (100 chars)" in t for t in texts),
                        texts)
        # the seal: 1.1's message freezes with ITS button the poll it
        # crossed the grid — and 1.2 is born below, still filling
        freezes = [(t, k) for t, k in self.edits if k]
        self.assertTrue(any("<b>CoT 1.1</b>" in t for t, _k in freezes))
        self.assertEqual(
            [b["text"] for f in freezes[0][1]["inline_keyboard"] for b in f],
            ["CoT 1.1"])
        self.assertTrue(any("filling CoT 1.2 (50 chars)" in t for t in texts))
        self.assertTrue(any("filling CoT 1.2 (400 chars)" in t
                            for t in texts + [t for t, _m in self.edits]))
        # the request ended: the tail seals as 1.2 with its button
        self.assertTrue(any("<b>CoT 1.2</b>" in t for t, _k in freezes))
        # the store's bulk settle agrees: no doubles, the cache is exact
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["labels"], ["CoT 1.1", "CoT 1.2"])
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500])
        self.assertTrue(all(d["phs"]))
        self.assertTrue(d["done"])

    def test_the_thinking_message_blinks_through_the_states(self):
        """F75 — Damian, 27/10: «además del cot, quiero ver un mensaje de  # noqa: spanish — Damian's request, quoted
        "thinking con cerebro" que significa que está procesando o  # noqa: spanish — Damian's request, quoted (continuation)
        trabajando pero sin un cot asociado aún». ONE message for the  # noqa: spanish — Damian's request, quoted (continuation)
        whole turn: 🧠 thinking (spinner + clock re-edit it — the blink),
        🔧 the tool RUNNING at the tail with its file (ZCode's status
        vocabulary), ✍️ while the answer writes, ✅ at the close — and
        never any counter («2 boxes sealed» is gone root and branch)."""
        from zcode_bot import live_io as lio
        stages = [
            None,   # request in flight, no flush yet: the cartel is born
            {"reasoningChars": 0, "textChars": 90,
             "reasoningTail": "", "textTail": "the <answer> so far"},
            None,   # the request ended: the tool state takes the line
        ]
        calls = [0]

        def fake_latest(sid, max_age=900.0, read=None):
            i = min(calls[0], len(stages) - 1)
            calls[0] += 1
            return stages[i]
        support.patch(self, lio, "latest", fake_latest)

        class _ProtoLive:
            """The store EVOLVES: bare at first (nothing in flight the
            bot can see — the cartel is all there is), then the running
            Edit at the tail (the tool state)."""
            def __init__(self, marker):
                self.marker, self.n = marker, 0
                self.bare = [_msg("user", {"type": "text", "text": "p"})]
                self.tool = self.bare + [
                    _msg("assistant", _r("a" * 500),
                         {"type": "tool", "tool": "Edit", "state": {
                             "status": "running",
                             "input": {"file_path":
                                       "/w/src/zcode_bot/reasoning.py"}}})]

            def request(self, method, params=None, timeout=None):
                if method == "session/messages":
                    self.n += 1
                    if self.n > 4:
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                    return {"result": {"messages":
                                       self.tool if self.n >= 3 else self.bare}}
                return {"result": {}}

            def close(self):
                pass

        support.patch(self, reasoning.zc_proto, "make",
                      lambda cwd=None, **kw: _ProtoLive(
                          reasoning.marker_path(SID)))
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        # born as the brain cartel, spinner and all
        born = next(t for t, _mk in self.sends if "thinking…" in t)
        self.assertIn("🧠", born)
        self.assertIn("«cv»", born)
        self.assertTrue(any(f in born for f in reasoning._THINK_FRAMES))
        # the answer writes: the ✍️/📝 blink (F80) with the readable
        # (escaped) tail — BOTH faces of the pair show across edits
        self.assertTrue(any("writing… (90 chars)" in t
                            for t, _m in self.edits))
        self.assertTrue(any("%s writing… (90 chars)" % face in t
                            for t, _m in self.edits
                            for face in ("✍️", "📝")))
        self.assertTrue(any("&lt;answer&gt;" in t for t, _m in self.edits))
        # the tool at the tail: the app's own status vocabulary, file
        # and all — F75b's full catalog (every tool gets its own words)
        self.assertTrue(any("Editing: reasoning.py…" in t for t, _m in self.edits))
        for act, want in [
                ({"tool": "Bash", "running": True, "file": "",
                  "detail": "cd ~/Projects/bot && git status"},
                 "Running Bash: cd ~/Projects/bot &amp;&amp; git status"),
                ({"tool": "Agent", "running": True, "file": "", "detail": ""},
                 "Delegating to an agent"),
                ({"tool": "Skill", "running": True, "file": "", "detail": ""},
                 "Loading a skill"),
                ({"tool": "Grep", "running": True, "file": "",
                  "detail": "def _watch_turn"}, "Searching: def _watch_turn"),
                ({"tool": "Whatever", "running": True, "file": "",
                  "detail": ""}, "Running Whatever"),
                ({"tool": "Edit", "running": True,
                  "file": "/w/src/zcode_bot/reasoning.py", "detail": ""},
                 "Editing: reasoning.py"),
        ]:
            self.assertIn(want, reasoning._live_line_text("t", T0, None, act))
        # and the close: plain, no counters of any kind
        closed = [t for t, _m in self.edits if "turn closed" in t]
        self.assertEqual(closed, ["✅ «cv» — turn closed"])

    def test_agent_view_mutes_the_live_stream(self):
        # F67 — while one of this session's agents is on VIEW, the parent's
        # live stream holds (picking a subagent = only that subagent
        # streams); the boxes still seal in the ledger and the cache keeps
        # them for /cot. F85a: NO closing bundle either — cots land in
        # the moment or wait in the cache; the collapsed landing is the
        # task-switch re-attach's alone.
        from zcode_bot import agent_live
        with open(agent_live.view_pid_path(SID), "w") as f:
            f.write("1234")
        proto = _ProtoCreciente(reasoning.marker_path(SID))
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        self.assertEqual(self.edits, [])   # nothing live: no box, no line
        bundles = [s for s in self.sends if s[1] and "cots landed" in s[0]]
        self.assertEqual(bundles, [])   # F85a: the close pushes nothing
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertEqual([len(c) for c in d["boxes"]], [CAP, 500, 300])

    def test_failed_resume_leaves_the_way_to_f40(self):
        proto = _ProtoCreciente(reasoning.marker_path(SID))

        def req(method, params=None, timeout=None):
            if method == "session/resume":
                return {"error": {"code": -1, "message": "boom"}}
            return {"result": {"events": []}}
        proto.request = req
        support.patch(self, reasoning.zc_proto, "make", lambda cwd=None, **kw: proto)
        reasoning.run_watcher(CFG, SID, "cv", T0, "/w")
        with open(reasoning.cache_path(SID, T0)) as f:
            d = json.load(f)
        self.assertTrue(d.get("done"))  # does not stay hung as the owner
        self.assertEqual(d["boxes"], [])


class TestSpawns(unittest.TestCase):
    """on_user_prompt spawns --cot-live; on_stop does NOT spawn
    --cot-buttons when the watchdog already owns it (cache exists)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.patch(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        support.patch(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None, a_outbox=True: True)
        self.zt.set_active_bot_session(
            {"sid": SID, "title": "cv", "ws": "/w"})
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        notify_prompt=True, mac_cot_buttons=True)

    def test_prompt_spawns_cot_live(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": SID, "cwd": "/w",
                                          "title": "cv", "prompt": "p"})
        live = [a for a in self.spawns if "--cot-live" in a]
        self.assertEqual(len(live), 1)
        i = live[0].index("--cot-live")
        self.assertEqual(live[0][i + 1], SID)
        self.assertEqual(live[0][i + 3], str(int(os.path.getmtime(
            self.zt.marker_path(SID)))))

    def test_knob_off_does_not_spawn(self):
        self.zt.on_user_prompt(dict(self.cfg, mac_cot_buttons=False),
                               {"sessionId": SID, "prompt": "p"})
        self.assertEqual([a for a in self.spawns if "--cot-live" in a], [])

    def _stop(self):
        self.zt.on_stop(dict(self.cfg, notify_stop=True),
                        {"sessionId": SID, "responseText": "done",
                         "cwd": "/w", "title": "cv"})

    def test_on_stop_skips_f40_if_the_live_one_owns_it(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": SID, "cwd": "/w",
                                          "title": "cv", "prompt": "p"})
        live = next(a for a in self.spawns if "--cot-live" in a)
        t0_live = int(live[live.index("--cot-live") + 3])
        # F51a: ownership by the live watcher = its pid-file (live
        # process) + the live=True cache it wrote — even if it is
        # summarizing (heartbeat expired), on_stop does not steal the
        # closing.
        reasoning.pid_write(SID)
        reasoning.save_cache(SID, t0_live, "cv", [], [], live=True)  # takes ownership
        self._stop()
        self.assertEqual([a for a in self.spawns if "--cot-buttons" in a], [])

    def test_on_stop_launches_f40_if_there_is_no_cache(self):
        with open(self.zt.marker_path(SID), "w") as f:
            f.write("{}")
        self._stop()
        self.assertEqual(len([a for a in self.spawns
                              if "--cot-buttons" in a]), 1)


if __name__ == "__main__":
    unittest.main()
