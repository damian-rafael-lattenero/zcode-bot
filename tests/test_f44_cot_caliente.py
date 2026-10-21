"""F44 — the per-session HOT Mac CoT watcher, and the single-button fix.

The single button (Damian, 20/10: "a single cot button appeared in the
whole conversation"): uhttp.send returned True and the three sources
stored it as the message_id of the button box → editMessageText(
message_id=True) failed silently and the box stayed stuck at «CoT 1».
The F41-F43 tests missed it because their send doubles returned an
integer. Here: the send contract (returns the id), ph_of discards a
True, and the sources keep the ph in EVERY cache rewrite (the tap edits
in place).

F44: ONE resume per SESSION — the watcher watches the turn that launched
it and stays hot waiting for the next marker of the same session (same
connection, no second resume); single-flight of the hook by pid-file
with heartbeat; after_id cut and monotonic guard; /nuclear finishes it
off.
"""
import json
import os
import signal
import sys
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot import nuclear as zc_nuclear  # noqa: E402
from zcode_bot.live_view import LiveTurnView  # noqa: E402
from zcode_bot.utils import http as zh  # noqa: E402

CFG = {"token": "T", "chat_id": 1, "enabled": True}
# F51c: recent — the long-turn notice fires with an old t0
# F51c: recent (120s ago: far from the second of the real
# turn 2 marker in the pickups, far from the notice threshold)
T0 = int(time.time()) - 120
CAP = rz.COT_BOX_CAP
A = "a" * (CAP + 500)   # one reasoning: 1 full box + a 500-char tail
B = "b" * 300           # another short reasoning
KEEP_SHORT = 0.05       # real keepalive that expires right away (patched sleep)


# --------------------------------------------------------------- helpers

def _msg(role, mid, *parts):
    return {"info": {"role": role, "id": mid}, "parts": list(parts)}


def _r(text):
    return {"type": "reasoning", "text": text}


_TXT = {"type": "text", "text": "end"}


class _ProtoScript:
    """session/messages returns the next step of the script; each step
    may carry the action the HOOK would perform on the marker at that
    moment: "rm" (on_stop consumed it) or ("new", token) (new prompt)."""

    def __init__(self, marker, steps):
        self.marker, self.steps = marker, list(steps)
        self.requests, self.resumes, self.closed = [], 0, False

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            self.resumes += 1
            return {"result": {}}
        if method == "session/messages":
            self.requests.append(dict(params or {}))
            msgs, action = self.steps.pop(0) if self.steps else ([], None)
            if action == "rm":
                try:
                    os.remove(self.marker)
                except OSError:
                    pass
            elif action:
                with open(self.marker, "w") as f:
                    json.dump({"token": action[1]}, f)
            return {"result": {"messages": msgs}}
        return {"result": {}}

    def close(self):
        self.closed = True


def _buttons(kb):
    return [b["text"] for row in (kb or {}).get("inline_keyboard", [])
            for b in row]


# ------------------------------------------------------ send contract

class TestSendContract(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.addCleanup(setattr, zh, "_cooldown_until", [0.0])
        zh._cooldown_until[0] = 0.0

    def test_send_returns_the_message_id(self):
        support.patch(self, zh, "tg_api",
                        lambda cfg, m, params=None, timeout=35:
                        {"ok": True, "result": {"message_id": 777}})
        self.assertEqual(zh.send(CFG, "hello"), 777)
        self.assertEqual(zh._live_send(CFG, "hello"), 777)

    def test_send_without_id_still_truthy(self):
        support.patch(self, zh, "tg_api",
                        lambda cfg, m, params=None, timeout=35: {"ok": True})
        self.assertIs(zh.send(CFG, "hello"), True)
        support.patch(self, zh, "tg_api",
                        lambda cfg, m, params=None, timeout=35: None)
        self.assertIs(zh.send(CFG, "hello"), True)

    def test_ph_of_discards_what_is_not_an_id(self):
        self.assertEqual(rz.ph_of(555), 555)
        self.assertIsNone(rz.ph_of(True))   # bool is a subclass of int
        self.assertIsNone(rz.ph_of(False))
        self.assertIsNone(rz.ph_of(None))


# ------------------------------------------------ celu: the ph is preserved

class _FakeC:
    def __init__(self, evs):
        self.evs = evs

    def request(self, method, params=None, timeout=None):
        return {"result": {"events": [dict(e) for e in self.evs]}}


class _Th:
    def __init__(self, n=2):
        self.n = n

    def is_alive(self):
        self.n -= 1
        return self.n > 0


def _ev(eid, kind, delta=""):
    p = {"kind": kind}
    if delta:
        p["delta"] = delta
    return {"eventId": eid, "payload": p}


# one reasoning that fills TWO boxes and closes by tool (tail = box 3)
_EVS = [_ev(1, "reasoning_delta", "x" * (2 * CAP + 400)),
        _ev(2, "tool_input_start"), _ev(3, "tool_call")]


class TestPhoneKeepsPh(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _view(self, send_ret):
        self.sends, self.edits = [], []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append((text, markup)), send_ret)[1],
            "live_send": lambda cfg, text: 620,
            "live_edit": lambda cfg, mid, text, markup=None:
                (self.edits.append((mid, text, markup)), True)[1],
            "sleep": lambda s: None, "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1, "rss_now": lambda: -1,
            "skip_mark": lambda sid: None,
        }
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1, cot_botones=True)
        return LiveTurnView(_FakeC(_EVS), cfg, support.T1, float(T0),
                            "testing", 777, _Th(2), deps=deps)

    async def test_every_cache_rewrite_carries_the_ph(self):
        v = self._view(601)
        v.run()
        self.assertEqual(v.cot_labels, ["CoT 1.1", "CoT 1.2", "CoT 1.3"])
        self.assertEqual(len(self.sends), 1)  # the box is born ONCE…
        # …and grows by editing ITS message_id: 2nd box, 3rd (tail) and
        # close of the reasoning — the 3 edits to the SAME id (F49: no
        # more 4th edit of the "final index"; the reasoning closes with
        # its own and that's it)
        self.assertEqual([e[0] for e in self.edits if e[2]],
                         [601, 601, 601])
        last = [e for e in self.edits if e[2]][-1]  # the close of the reasoning
        self.assertEqual(_buttons(last[2]), ["CoT 1.1", "CoT 1.2", "CoT 1.3"])
        self.assertIn("pass 1", last[1])
        with open(rz.cache_path(support.T1, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["ph"], 601)        # before: it was lost on the 2nd box
        self.assertTrue(d["finalizado"])      # and the final didn't carry it either
        self.assertEqual(len(d["cajas"]), 3)

    async def test_a_send_true_is_not_used_as_message_id(self):
        v = self._view(True)  # the old contract: it went out, but with no id
        v.run()
        self.assertIsNone(v.cot_ph)
        self.assertFalse(any(e[0] is True for e in self.edits))  # the bug
        with open(rz.cache_path(support.T1, T0)) as f:
            self.assertNotIn("ph", json.load(f))


# ------------------------------------------------ Mac: hot watcher

class _WatcherBase(unittest.TestCase):
    SID = support.T1  # in the tasks index as "testing"

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits, self.naps = [], [], []
        support.patch(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), 777)[1])
        support.patch(self, rz.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((ph, text, markup)), True)[1])
        support.patch(self, rz.time, "sleep", lambda s: self.naps.append(s))
        self.marker = rz.marker_path(self.SID)
        with open(self.marker, "w") as f:
            json.dump({"token": "tok1"}, f)

    def _run(self, steps, keepalive, cfg=None):
        proto = _ProtoScript(self.marker, steps)
        support.patch(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.run_vigilante(cfg or CFG, self.SID, "cv", T0, "/w", keepalive=keepalive)
        return proto

    def _caches(self):
        st = self.zt.config.STATE_DIR
        out = {}
        for n in sorted(os.listdir(st)):
            if n.startswith("cot-%s-" % self.SID) and n.endswith(".json"):
                with open(os.path.join(st, n)) as f:
                    d = json.load(f)
                out[d["t0"]] = d
        return out


class TestWarmWatcher(_WatcherBase):
    def test_next_turn_pickup_without_second_resume(self):
        steps = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], None),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            # final fetch of turn 1; at that instant the hook writes the
            # turn 2 marker (new prompt on the Mac)
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A)),
              _msg("assistant", "m3", _TXT)], ("new", "tok2")),
            # turn 2: only what comes after m3 (afterMessageId)
            ([_msg("user", "m4", _TXT), _msg("assistant", "m5", _r(B))], "rm"),
            ([_msg("user", "m4", _TXT), _msg("assistant", "m5", _r(B), _TXT)],
             None),
        ]
        proto = self._run(steps, keepalive=KEEP_SHORT)
        self.assertEqual(proto.resumes, 1)  # the key to F44
        caches = self._caches()
        self.assertEqual(len(caches), 2)
        d1 = caches[T0]
        self.assertEqual([len(c) for c in d1["cajas"]], [CAP, 500])
        self.assertTrue(d1["finalizado"])
        self.assertEqual(d1["ph"], 777)        # F43c: the tap edits in place
        d2 = next(d for t, d in caches.items() if t != T0)
        self.assertEqual([len(c) for c in d2["cajas"]], [300])
        self.assertTrue(d2["finalizado"])
        self.assertEqual(d2["titulo"], "testing")  # recomputed from the index
        # turn 2 was read AFTER the last message of turn 1: at pickup the
        # new prompt may not be persisted yet, and without the cut the
        # last user would be the one from the previous turn
        self.assertNotIn("afterMessageId", proto.requests[0])
        self.assertEqual(proto.requests[3]["afterMessageId"], "m3")
        self.assertEqual(proto.requests[4]["afterMessageId"], "m3")
        # two button boxes (one per turn) and their own closes (F49:
        # «pass R — N boxes», no longer a «Razonamiento» index with them all)
        self.assertEqual(len(self.sends), 2)
        finals = [t for _p, t, _k in self.edits if "pass" in t]
        self.assertIn("2 boxes", finals[0])
        self.assertIn("1 box", finals[1])
        self.assertFalse(any("Reasoning" in t for _p, t, _k in self.edits))
        # when the keepalive expires it lets go of everything: pid-file gone, runtime closed
        self.assertFalse(os.path.exists(rz.pid_path(self.SID)))
        self.assertTrue(proto.closed)

    def test_keepalive_zero_one_turn_and_done(self):
        steps = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             ("new", "tok2")),  # another prompt arrives: without keepalive it isn't mine
        ]
        proto = self._run(steps, keepalive=0)
        self.assertEqual(len(self._caches()), 1)
        self.assertTrue(proto.closed)
        self.assertTrue(all(s == rz.COT_LIVE_POLL_S for s in self.naps))

    def test_stop_while_idle_releases_the_runtime_without_consuming_the_marker(self):
        steps = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             None),
        ]
        self.zt._stop_request(self.SID)
        proto = self._run(steps, keepalive=3600)  # it would hang for an hour
        self.assertTrue(proto.closed)
        self.assertEqual(len(self._caches()), 1)
        # the /stop marker belongs to the runtime owner: here it is only looked at
        self.assertTrue(os.path.exists(self.zt._stop_file(self.SID)))

    def test_new_code_deployed_releases_the_runtime(self):
        # F48d: the hot watcher also steps aside when it sees the deploy —
        # the hook spawns a new one (new code) on the next Mac turn
        steps = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             None),
        ]
        support.patch(self, rz.config, "code_stamp", lambda: "other")
        proto = self._run(steps, keepalive=3600)  # it would hang for an hour
        self.assertTrue(proto.closed)
        self.assertEqual(len(self._caches()), 1)
        self.assertFalse(os.path.exists(rz.pid_path(self.SID)))

    def test_task_not_current_or_bot_worker_release(self):
        steps = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             None),
        ]
        cfg = dict(CFG, only_active_task=True)
        self.zt.set_active_bot_session({"sid": support.T2, "title": "CV",
                                        "ws": "/w"})
        proto = self._run(steps, keepalive=3600, cfg=cfg)
        self.assertTrue(proto.closed)
        # and with the task current but a worker of the bot owning the
        # session (celu took it: its LiveTurnView already does the boxes)
        self.zt.set_active_bot_session({"sid": self.SID, "title": "testing",
                                        "ws": "/w"})
        self.zt._workers().register(self.SID, {"titulo": "t", "pid": 1})
        with open(self.marker, "w") as f:
            json.dump({"token": "tok3"}, f)
        proto2 = self._run([(p[0], p[1]) for p in steps], keepalive=3600,
                           cfg=cfg)
        self.assertTrue(proto2.closed)

    def test_monotonic_guard_never_less_than_what_was_shown(self):
        steps = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], None),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([], None),  # the final fetch came back empty
        ]
        self._run(steps, keepalive=0)
        d = self._caches()[T0]
        self.assertEqual([len(c) for c in d["cajas"]], [CAP])  # what was seen live stays
        self.assertTrue(d["finalizado"])
        self.assertIn("1 box", self.edits[-1][1])

    def test_marker_replaced_midway_chains_the_new_turn(self):
        """Race of on_stop → new prompt between two polls: turn 1 closes
        with what was seen live (the history already belongs to the next
        turn) and turn 2 is watched immediately — even with keepalive 0,
        because the connection is already up and the turn already runs."""
        steps = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))],
             ("new", "tok2")),
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B))], "rm"),
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B), _TXT)],
             None),
        ]
        proto = self._run(steps, keepalive=0)
        self.assertEqual(proto.resumes, 1)
        caches = self._caches()
        self.assertEqual(len(caches), 2)
        self.assertEqual([len(c) for c in caches[T0]["cajas"]], [CAP])
        d2 = next(d for t, d in caches.items() if t != T0)
        self.assertEqual([len(c) for c in d2["cajas"]], [300])
        self.assertEqual(len(proto.requests), 3)  # no final fetch of turn 1

    def test_pickup_without_new_history_re_resumes_once(self):
        """Safety net against a connection that doesn't reflect the store
        after the pickup: if after after_id NEITHER the new prompt shows
        up in 5 polls, a session/resume (the usual cost, only here)
        re-attaches it."""
        turn1 = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             ("new", "tok2")),
        ]
        empty_polls = [([], None)] * rz.COT_LIVE_EMPTY_POLLS
        turn2 = [
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B))], "rm"),
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B), _TXT)],
             None),
        ]
        proto = self._run(turn1 + empty_polls + turn2, keepalive=KEEP_SHORT)
        self.assertEqual(proto.resumes, 2)
        d2 = next(d for t, d in self._caches().items() if t != T0)
        self.assertEqual([len(c) for c in d2["cajas"]], [300])
        # all turn 2 fetches (5 empty + 1 with data + the final one)
        # read AFTER the last message of turn 1
        self.assertEqual(sum(1 for p in proto.requests
                             if p.get("afterMessageId") == "m2"),
                         rz.COT_LIVE_EMPTY_POLLS + 2)

    def test_other_turn_by_token_and_tolerant_of_mid_write_marker(self):
        self.assertFalse(rz._other_turn((10, "tok1"), (10, "")))   # mid-write
        self.assertFalse(rz._other_turn((10, "tok1"), (10, "tok1")))
        self.assertTrue(rz._other_turn((10, "tok1"), (10, "tok2")))  # same second
        self.assertTrue(rz._other_turn((10, ""), (11, "")))         # no tokens: mtime
        with open(self.marker, "w") as f:
            f.write('{"token": "tok')  # truncated
        mt, tok = rz._marker_state(self.SID)
        self.assertEqual(tok, "")
        self.assertEqual(mt, int(os.path.getmtime(self.marker)))
        os.remove(self.marker)
        self.assertIsNone(rz._marker_state(self.SID))

    def test_successor_in_the_pid_file_stands_the_watcher_down(self):
        steps = [([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))],
                  None)] * 3

        def send(cfg, text, loud=None, markup=None):
            # with the box already born, a successor takes over the pid-file
            with open(rz.pid_path(self.SID), "w") as f:
                f.write(str(support.FAKE_PID))
            return 777
        support.patch(self, rz.uhttp, "send", send)
        proto = self._run(steps, keepalive=3600)
        self.assertTrue(proto.closed)
        self.assertEqual(len(proto.requests), 1)  # it stepped aside on the next poll
        # the pid-file belongs to the successor: it isn't deleted
        self.assertEqual(rz.pid_read(self.SID), support.FAKE_PID)


# ------------------------------------------------ hook: single-flight

class TestSingleFlightHook(unittest.TestCase):
    SID = support.T1

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.patch(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        support.patch(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None: True)
        self.zt.set_active_bot_session({"sid": self.SID, "title": "testing",
                                        "ws": "/w"})
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        mac_cot_botones=True)

    def _prompt(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": self.SID, "cwd": "/w",
                                          "prompt": "p"})
        return [a for a in self.spawns if "--cot-vivo" in a]

    def test_watcher_alive_and_fresh_spawns_no_other(self):
        rz.pid_write(self.SID)  # this process, heartbeat just now
        self.assertEqual(self._prompt(), [])
        self.assertTrue(rz.watcher_alive(self.SID))

    def test_dead_watcher_spawns(self):
        with open(rz.pid_path(self.SID), "w") as f:
            f.write(str(support.FAKE_PID))  # impossible pid on macOS
        self.assertFalse(rz.watcher_alive(self.SID))
        self.assertEqual(len(self._prompt()), 1)

    def test_stale_heartbeat_is_a_hang_and_spawns(self):
        rz.pid_write(self.SID)
        old = time.time() - rz.COT_LIVE_HB_FRESH_S - 5
        os.utime(rz.pid_path(self.SID), (old, old))
        self.assertFalse(rz.watcher_alive(self.SID))
        self.assertEqual(len(self._prompt()), 1)
        # and the heartbeat makes it fresh again
        rz.pid_beat(self.SID)
        self.assertTrue(rz.watcher_alive(self.SID,
                                         proc_alive=lambda pid: True))

    def test_without_pid_file_spawns(self):
        self.assertEqual(len(self._prompt()), 1)


# ------------------------------------------------ /nuclear finishes them off

class TestNuclearFinishesWatchers(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.signals = []

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError(pid)  # already dead: the finisher doesn't wait
            self.signals.append((pid, sig))
        support.patch(self, zc_nuclear.os, "kill", fake_kill)
        support.patch(self, zc_nuclear.time, "sleep", lambda s: None)
        with open(rz.pid_path(support.T1), "w") as f:
            f.write(str(support.FAKE_PID))

    def test_sigterm_to_the_watcher_and_cleans_the_pid_file(self):
        self.zt._nuc_kill_own_runtimes(es_cot_vivo=lambda pid: True)
        self.assertIn((support.FAKE_PID, signal.SIGTERM), self.signals)
        self.assertFalse(os.path.exists(rz.pid_path(support.T1)))

    def test_recycled_pid_untouched_but_the_file_cleaned(self):
        self.zt._nuc_kill_own_runtimes(es_cot_vivo=lambda pid: False)
        self.assertEqual(self.signals, [])
        self.assertFalse(os.path.exists(rz.pid_path(support.T1)))


if __name__ == "__main__":
    unittest.main()
