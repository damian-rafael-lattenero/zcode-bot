"""F21 — the promise kept: no turn dies in silence.

Two paths covered:
1. (HISTORICAL, R0b made it impossible) Worker ABANDONS at 40 min:
   today the worker does NOT abandon — the wait for the end has no cap
   ("⏱️ lleva N min" notice every 30) and the only cut is /stop. The
   abandonment KILLED the runtime ("Turn was cancelled" when the worker
   exited, 20/10 14:55) — see TestTurnEndPollNoCap.
2. Worker DIES abruptly (kill -9/OOM, no finally): the entry is left
   with a dead pid → supervisor (_orphans_check) detects it → delivers
   (or an honest "murió").

F22bis: permanent tool boxes (full command + diffs), Damian's request:
"see the called tools, the full commands and the modified files with
the diff, in addition to the CoT".
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))
from zcode_bot import config as zc_config  # noqa: E402


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


class _FakeProc:
    def __init__(self):
        self.returncode = None  # alive

    def poll(self):
        return self.returncode


class TestTurnEndPollNoCap(unittest.TestCase):
    """R0b — the worker does NOT abandon long turns: _turn_end_poll with
    timeout=None keeps waiting beyond the old 40 min (when abandonment
    killed the runtime: turn.failed "Turn was cancelled" the very same
    second the worker exited — CLI log 20/10 14:55) and gives a notice
    every 30 min of elapsed turn while it waits."""

    def test_keeps_going_past_40_and_notifies_every_30(self):
        from zcode_bot import live_view as lv

        class _Reloj:  # 15 min per lap: crosses 30 and 60 before the end
            now = 1000.0

            def time(self):
                type(self).now += 900.0
                return self.now

            def sleep(self, s):  # poll pacing: doesn't advance the clock
                pass

        class _Conn:
            n = 0

            def request(self, method, params=None, timeout=None):
                type(self).n += 1
                evs = [{"seq": self.n, "type": "turn.started",
                        "payload": {"inputId": "in1",
                                    "executionStartedAt": 1_001_000}}]
                if self.n >= 5:  # the turn ends at around minute ~75
                    evs.append({"seq": self.n + 1, "type": "turn.completed",
                                "payload": {"inputId": "in1",
                                            "resultType": "success"}})
                return {"result": {"events": evs}}

        clock = _Reloj()
        support.patch(self, lv, "time", clock)
        notices = []
        end = lv._turn_end_poll("sid", t0=950.0, timeout=None, t_send=1000.0,
                                conn=_Conn(),
                                aviso=lambda m: notices.append(m))
        # with the old cap (1800+600) it would have returned None at ~30-40 min
        self.assertEqual(end["event"], "turn.completed")
        self.assertEqual(notices, [30, 60])


class _FakeClient:
    def __init__(self):
        self.p = _FakeProc()
        self.closed = False

    def close(self):
        self.closed = True


class TestActiveTurn(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_register_y_clear(self):
        zt = self.zt
        zt._active_turn_register("sess_X", "testing", 123.0,
                                 ws="/w", last_id="m1")
        entry = zt._store("bot-active.json").read()["sess_X"]
        self.assertEqual(entry["titulo"], "testing")
        self.assertEqual(entry["last_id"], "m1")
        self.assertEqual(entry["pid"], os.getpid())
        zt._active_turn_clear("sess_X")
        self.assertEqual(zt._store("bot-active.json").read(), {})

    async def test_register_without_sid_is_noop(self):
        self.zt._active_turn_register(None, "t", 1.0)
        self.assertIsNone(self.zt._store("bot-active.json").read())


class TestOrphansCheck(unittest.IsolatedAsyncioTestCase):
    """The supervisor: dead pid + end → delivers; with no end → honest."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.T1 = support.T1
        self.sends = []
        # F4g: _orphans_check lives in workers (uhttp.*, its own bindings)
        from zcode_bot import workers as zc_workers
        from zcode_bot.utils import http as zc_http
        ps = mock.patch.object(
            zc_http, "send",
            lambda cfg, text, loud=None, markup=None:
                (self.sends.append(text), True)[1])
        ps.start()
        self.addCleanup(ps.stop)
        self.replies = []

        def fake_long(cfg, title, dur, text, max_partes=3):
            self.replies.append((title, text))
        pl = mock.patch.object(zc_workers, "_send_long_reply", fake_long)
        pl.start()
        self.addCleanup(pl.stop)

    def _register_dead(self, sid="sess_X", t0=None, ws=None):
        t0 = t0 or (time.time() - 60)
        self.zt._active_turn_register(sid, "testing", t0, ws=ws)
        # a truly dead pid (process already finished)
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()

        def fn(q):
            q[sid]["pid"] = p.pid
            return q
        self.zt._store("bot-active.json").update_cross_process(fn, {})
        return t0

    def _log_end(self, sid, event, t0):
        log_today = os.path.join(
            zc_config.BASE, "cli", "log",
            "zcode-%s.jsonl" % time.strftime("%Y-%m-%d"))
        # the end arrives AFTER the turn registration (since_epoch=t0)
        with open(log_today, "a") as f:
            f.write(json.dumps({"timestamp": support._iso(t0 + 5),
                                "event": event, "sessionId": sid,
                                "turnId": "turn_X"}) + "\n")

    async def test_dead_without_end_notifies_honestly(self):
        zt = self.zt
        self._register_dead()
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("died with its worker" in s for s in self.sends))
        self.assertIsNone((zt._store("bot-active.json").read() or {})
                          .get("sess_X"))

    async def test_dead_with_completed_delivers_text(self):
        zt = self.zt
        t0 = time.time() - 120
        self._register_dead(t0=t0)
        self._log_end("sess_X", "turn.completed", t0)
        class FakeC:
            def request(self, method, params=None, timeout=None):
                if method == "session/resume":
                    return {"result": {}}
                if method == "session/messages":
                    return {"result": {"messages": [
                        {"info": {"role": "assistant", "id": "m2"},
                         "parts": [{"type": "text",
                                    "text": "the orphan's reply"}]}]}}
                return {}

        def fake_make(cwd=None, **kw):
            return FakeC()
        from zcode_bot import workers as zc_workers
        support.patch(self, zc_workers, "zc_proto",
                        type("M", (), {"make": staticmethod(fake_make)}))
        zt._orphans_check(_cfg(zt))
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(self.replies[0][1], "the orphan's reply")

    async def test_dead_with_failed_notifies_failure(self):
        zt = self.zt
        t0 = time.time() - 120
        self._register_dead(t0=t0)
        self._log_end("sess_X", "turn.failed", t0)
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("failed" in s for s in self.sends))
        self.assertFalse(any("TERMINÓ" in s for s in self.sends))

    async def test_alive_does_nothing(self):
        zt = self.zt
        zt._active_turn_register("sess_X", "t", time.time() - 10)
        zt._orphans_check(_cfg(zt))
        self.assertEqual(self.sends, [])
        self.assertIsNotNone((zt._store("bot-active.json").read() or {})
                             .get("sess_X"))

    async def test_purge_by_age(self):
        zt = self.zt
        self._register_dead(t0=time.time() - 7201)
        # dead pid BUT an entry older than 2h: silent purge, no notice
        zt._orphans_check(_cfg(zt))
        self.assertEqual(self.sends, [])
        self.assertIsNone((zt._store("bot-active.json").read() or {})
                          .get("sess_X"))

    # ---- F47c: the /stop that finishes off the worker counts as /stop, not failure

    async def test_failed_after_stop_says_stopped_not_failed(self):
        # /stop expires the grace period → kill -9 → the worker never runs
        # its finally → dead pid + turn.failed: before it reported
        # «❌ falló (el worker había muerto)» for something the user had
        # asked for (20/10 13:03)
        from zcode_bot import workers as zc_workers
        zt = self.zt
        t0 = time.time() - 120
        self._register_dead(t0=t0)
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        self._log_end("sess_X", "turn.failed", t0)
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("stopped by your /stop" in s for s in self.sends))
        self.assertFalse(any("failed" in s or "had died" in s
                             for s in self.sends))
        self.assertIsNone((zt._store("bot-active.json").read() or {})
                          .get("sess_X"))

    async def test_no_end_after_stop_says_stopped_not_oom(self):
        from zcode_bot import workers as zc_workers
        zt = self.zt
        self._register_dead()
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        zt._orphans_check(_cfg(zt))
        self.assertTrue(any("stopped dead by your /stop" in s for s in self.sends))
        self.assertFalse(any("kill/OOM" in s for s in self.sends))

    async def test_completed_after_stop_delivers_anyway(self):
        # the stop arrived late and the turn finished: the reply is delivered
        from zcode_bot import workers as zc_workers
        zt = self.zt
        t0 = time.time() - 120
        self._register_dead(t0=t0)
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        self._log_end("sess_X", "turn.completed", t0)
        support.patch(self, zc_workers, "_orphan_fetch_text",
                        lambda ent, sid: "arrived anyway")
        zt._orphans_check(_cfg(zt))
        self.assertEqual([r[1] for r in self.replies], ["arrived anyway"])
        self.assertFalse(any("frenada" in s for s in self.sends))

    async def test_mark_without_entry_creates_none(self):
        from zcode_bot import workers as zc_workers
        zc_workers._active_turn_mark("sess_nada", stop=1.0)
        self.assertFalse((self.zt._store("bot-active.json").read() or {})
                         .get("sess_nada"))

    # ---- F47d: the CoT cache of the dead turn gets closed

    async def test_cot_cache_ends_up_closed_and_whole(self):
        # the worker's last save was vivo=True; with the worker dead nobody
        # closed it and the JSON said «vivo» forever
        from zcode_bot import razonamiento as rz
        zt = self.zt
        t0 = time.time() - 120
        self._register_dead(t0=t0)
        rz.save_cache("sess_X", t0, "testing", ["caja"], ["CoT 1.1"],
                      vivo=True, ph=5, phs=[5])
        zt._orphans_check(_cfg(zt))
        with open(rz.cache_path("sess_X", t0)) as f:
            d = json.load(f)
        self.assertNotIn("vivo", d)
        self.assertTrue(d["finalizado"])
        self.assertIn("kill/OOM", d["error"])
        # what the tap needs stays intact
        self.assertEqual((d["cajas"], d["rotulos"], d["ph"], d["phs"]),
                         (["caja"], ["CoT 1.1"], 5, [5]))

    async def test_cot_cache_after_stop_says_stopped(self):
        from zcode_bot import razonamiento as rz
        from zcode_bot import workers as zc_workers
        zt = self.zt
        t0 = time.time() - 120
        self._register_dead(t0=t0)
        zc_workers._active_turn_mark("sess_X", stop=time.time())
        rz.save_cache("sess_X", t0, "testing", ["caja"], ["CoT 1.1"], vivo=True)
        zt._orphans_check(_cfg(zt))
        with open(rz.cache_path("sess_X", t0)) as f:
            d = json.load(f)
        self.assertEqual(d["error"], "stopped by /stop")
        self.assertNotIn("vivo", d)

    async def test_without_cache_it_invents_nothing(self):
        from zcode_bot import razonamiento as rz
        t0 = time.time() - 120
        self._register_dead(t0=t0)
        self.zt._orphans_check(_cfg(self.zt))
        self.assertFalse(os.path.exists(rz.cache_path("sess_X", t0)))


class TestCloseCache(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_without_file_returns_false(self):
        from zcode_bot import razonamiento as rz
        self.assertFalse(rz.close_cache("sess_X", 1))
        self.assertFalse(os.path.exists(rz.cache_path("sess_X", 1)))

    def test_closes_without_touching_the_rest(self):
        from zcode_bot import razonamiento as rz
        rz.save_cache("sess_X", 1, "t", ["a", "b"], ["CoT 1.1", "CoT 1.2"],
                      vivo=True, ph=9, phs=[9, 9])
        self.assertTrue(rz.close_cache("sess_X", 1))
        with open(rz.cache_path("sess_X", 1)) as f:
            d = json.load(f)
        self.assertEqual(d, {"sid": "sess_X", "titulo": "t", "t0": 1,
                             "cajas": ["a", "b"],
                             "rotulos": ["CoT 1.1", "CoT 1.2"],
                             "ph": 9, "phs": [9, 9], "finalizado": True})

    def test_broken_json_returns_false(self):
        from zcode_bot import razonamiento as rz
        os.makedirs(zc_config.STATE_DIR, exist_ok=True)
        with open(rz.cache_path("sess_X", 1), "w") as f:
            f.write("{broken")
        self.assertFalse(rz.close_cache("sess_X", 1))


class TestDeadRuntime(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    async def test_poll_none_is_alive(self):
        zt = self.zt
        self.assertFalse(zt._runtime_muerto(_FakeClient()))

    async def test_poll_number_is_dead(self):
        zt = self.zt
        c = _FakeClient()
        c.p.returncode = -9
        self.assertTrue(zt._runtime_muerto(c))

    async def test_without_proc_no_death_verdict(self):
        # without .p there is no evidence: conservative = alive (don't fire
        # false "murió" on clients shaped some other way)
        self.assertFalse(self.zt._runtime_muerto(object()))


class TestToolBoxes(unittest.IsolatedAsyncioTestCase):
    """F22bis — the request: full tools, full commands, diffs."""

    def setUp(self):
        self.zt = support.fresh(self)

    async def test_bash_full_command(self):
        caja = self.zt._tool_caja("Bash", {"command": "git status && git log"})
        self.assertIn("git status &amp;&amp; git log", caja)
        self.assertIn("🔧 <b>Bash</b>", caja)

    async def test_edit_diff_real(self):
        caja = self.zt._tool_caja(
            "Edit", {"file_path": "/x/y.py",
                     "old_string": "a = 1\nb = 2",
                     "new_string": "a = 42\nb = 2"})
        # F7c+: diff in the style of git diff — 🟢 adds / 🔴 deletes, one
        # per line, with a totals diffstat on the first line (● 🟢 +1 🔴 −1)
        self.assertIn("● 🟢 +1  🔴 −1", caja)
        self.assertIn("🔴 -a = 1", caja)
        self.assertIn("🟢 +a = 42", caja)
        self.assertIn("y.py", caja)

    async def test_multiedit_one_diff_per_edit(self):
        # before it fell to the raw-JSON fallback («lo vi todo junto»)
        caja = self.zt._tool_caja(
            "MultiEdit", {"file_path": "/x/y.py", "edits": [
                {"old_string": "a = 1", "new_string": "a = 42"},
                {"old_string": "b = 2", "new_string": "b = 7"}]})
        self.assertIn("edit 1", caja)
        self.assertIn("edit 2", caja)
        self.assertIn("● 🟢 +1  🔴 −1", caja)  # diffstat per edit
        self.assertIn("🟢 +a = 42", caja)
        self.assertIn("🔴 -b = 2", caja)
        self.assertNotIn('"edits"', caja)  # no es JSON crudo

    async def test_write_counts_lines(self):
        caja = self.zt._tool_caja(
            "Write", {"file_path": "/x/nuevo.py",
                      "content": "linea1\nlinea2\nlinea3"})
        self.assertIn("🟢 linea1", caja)
        self.assertIn("🟢 +3 · 3 lines", caja)  # F50b: the stamp

    async def test_box_filter_by_preset(self):
        # 'accion' (default): Read index only, Bash/Edit/Agent/MCP with a box
        on = self.zt._tool_caja_on
        self.assertFalse(on("Read", {}))
        self.assertFalse(on("TodoWrite", {}))
        self.assertFalse(on("Grep", {}))
        self.assertTrue(on("Bash", {}))
        self.assertTrue(on("Edit", {}))
        self.assertTrue(on("Agent", {}))
        self.assertTrue(on("mcp__serper__search_web", {}))
        # 'todas': F7 status quo
        self.assertTrue(on("Read", {"tools_caja_filtro": "todas"}))
        # 'minimo': only bash + edits
        self.assertFalse(on("Agent", {"tools_caja_filtro": "minimo"}))
        self.assertFalse(on("mcp__serper__search_web",
                            {"tools_caja_filtro": "minimo"}))
        self.assertTrue(on("Write", {"tools_caja_filtro": "minimo"}))

    async def test_cap_3400(self):
        caja = self.zt._tool_caja("Bash", {"command": "x" * 20000})
        self.assertLess(len(caja), support.CH_CAJA + 800)  # format + header

    async def test_output_truncated(self):
        caja = self.zt._tool_caja(
            "Bash", {"command": "ls", "output": "a\nb\nc"})
        self.assertIn("└", caja)
        self.assertIn("a\nb\nc", caja)

    async def test_tools_of_messages_shape(self):
        msgs = [{"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "tool": "Bash", "callID": "call_1",
             "state": {"input": {"command": ["echo", "hola"]},
                       "status": "completed"}},
            {"type": "text", "text": "x"}]}]
        tools = self.zt._tools_of_messages(msgs)
        self.assertEqual(tools, [("Bash", {"command": ["echo", "hola"]})])
        # F7c: the callID variant pairs up with the stream's toolCallId
        self.assertEqual(self.zt._tools_con_call_id(msgs),
                         [("call_1", "Bash", {"command": ["echo", "hola"]},
                           "completed")])
        # compat: _steps_of_messages still returns (name, desc)
        self.assertEqual(self.zt._steps_of_messages(msgs),
                         [("Bash", "echo hola")])

    async def test_stream_boxes_can_be_turned_off(self):
        # F4d: _cajas_paso_nuevas lives in live_view and uses the transport
        # by attribute (uhttp.send) — the patch seam is the owning module
        from unittest import mock
        from zcode_bot.utils import http as zc_http
        sent_texts = []
        with mock.patch.object(zc_http, "send",
                               lambda cfg, text, loud=None, markup=None:
                               (sent_texts.append(text), True)[1]):
            self.zt._cajas_paso_nuevas(_cfg(self.zt), "t", [0],
                                       [(None, "Bash", {"command": "ls"},
                                         None)])
            self.assertEqual(len(sent_texts), 1)
            self.zt._cajas_paso_nuevas(_cfg(self.zt, tools_stream=False),
                                       "t", [0],
                                       [(None, "Bash", {"command": "ls"},
                                         None)])
            self.assertEqual(len(sent_texts), 1)  # unchanged
            # F7c: the tool with its own live box (born 🔵 from the stream)
            # isn't re-sent as a full box
            self.zt._cajas_paso_nuevas(_cfg(self.zt), "t", [0],
                                       [("call_9", "Edit",
                                         {"file_path": "a"}, "completed")],
                                       boxed={"call_9"})
            self.assertEqual(len(sent_texts), 1)  # still unchanged


if __name__ == "__main__":
    unittest.main()
