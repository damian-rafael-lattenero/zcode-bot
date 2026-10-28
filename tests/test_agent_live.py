"""F63 — the inside of each subagent, in boxes.

The rendering (REAL rollout record shape: querySource subagent, response
carrying reasoningText — the F33 lesson: fixtures are transcriptions),
the spawn handoff + FIFO assignment, the sealing/freezing contract, the
close on finishReason stop, the summary degradation and the tap plumbing
(_cmd_agb in-place expansion, _ag_rows vocabulary rewrite).
"""
import datetime
import json
import os
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import agent_live  # noqa: E402
from zcode_bot import config  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
from zcode_bot.commands import callbacks as cb  # noqa: E402
from zcode_bot.commands import handlers_info as hi  # noqa: E402

SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"
T0 = 1760000000
CFG = {"token": "T", "chat_id": 1}


def _iso(ms):
    return datetime.datetime.fromtimestamp(
        ms / 1000.0, tz=datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z")


def _rec(ms, reason="tool-calls", reasoning="", text="", tools=()):
    """A subagent rollout record — the REAL shape (startedAt ISO-Z string,
    response with reasoningText/text/toolCalls)."""
    return {"querySource": "subagent", "sessionId": "sess_subagent_agent_x",
            "startedAt": _iso(ms), "turnId": "t-agent",
            "response": {"finishReason": reason, "text": text,
                         "reasoningText": reasoning,
                         "toolCalls": [{"name": n, "input": i}
                                       for n, i in tools]}}


class _Transport:
    """send/edit/delete recorder: sends return incrementing message ids."""

    def __init__(self):
        self.sent, self.edits, self.deleted = [], [], []
        self._next = 7000

    def send(self, cfg, text, loud=None, markup=None):
        self._next += 1
        self.sent.append((self._next, text, markup))
        return self._next

    def edit(self, cfg, ph, text, markup=None):
        self.edits.append((ph, text, markup))
        return True

    def delete(self, cfg, ph):
        self.deleted.append(ph)


def _read_file(p, pos):
    """spectator._read_new equivalent over a WHOLE in-memory mapping of
    path → content (the tests write real files; this reads them all)."""
    try:
        with open(p, "rb") as f:
            data = f.read()
    except OSError:
        return [], pos
    if len(data) <= pos:
        return [], pos
    body = data[pos:]
    if not body.endswith(b"\n"):
        cut = body.rfind(b"\n")
        if cut < 0:
            return [], pos
        body = body[:cut + 1]
    out = []
    for ln in body.splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out, pos + len(body)


class TestRender(unittest.TestCase):
    def test_mixed_activity_chunk(self):
        chunk = agent_live.render_record(_rec(
            1000.0, reasoning="checking the parser first",
            text="Parser looks fine.",
            tools=[("Bash", {"command": "git log --oneline -3"}),
                   ("WebFetch", {"url": "https://x.dev"})]))
        self.assertIn("checking the parser first", chunk)
        self.assertIn("💬 Parser looks fine.", chunk)
        self.assertIn("🔧 Bash git log --oneline -3", chunk)
        self.assertIn("🔧 WebFetch https://x.dev", chunk)

    def test_monster_reasoning_gets_head_and_tail_not_all(self):
        big = "R" * (agent_live.REASON_KEEP + agent_live.REASON_HEAD + 9000)
        chunk = agent_live.render_record(_rec(1000.0, reasoning=big))
        self.assertLess(len(chunk), agent_live.REASON_KEEP + 1000)
        self.assertIn("[+", chunk)  # the cut is explicit

    def test_empty_record_renders_nothing(self):
        self.assertEqual(agent_live.render_record(_rec(1000.0)), "")


class TestTurnAgents(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.tr = _Transport()
        self.deps = {"send": self.tr.send, "edit": self.tr.edit,
                     "delete": self.tr.delete, "read_new": _read_file,
                     "now": lambda: T0 + 60}
        self.ta = agent_live.TurnAgents(CFG, SID, "title", float(T0),
                                        deps=dict(self.deps))

    def _spawn_entry(self, label="Explore", ts=None):
        with open(agent_live.spawn_log_path(SID), "a") as f:
            f.write(json.dumps({"ts": ts or (T0 + 5), "label": label,
                                "prompt": "verify the claims"}) + "\n")

    def _agent_file(self, name, recs):
        d = os.path.dirname(config.rollout_path("x"))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return p

    def test_spawn_births_live_message_with_the_label(self):
        self._spawn_entry()
        self.ta.poll_spawns()
        self.assertEqual(len(self.ta.agents), 1)
        _ph, text, _kb = self.tr.sent[0]
        self.assertIn("Agent 1", text)
        self.assertIn("Explore", text)
        self.assertIn("verify the claims", text)

    def test_heartbeat_reads_the_live_sidecar(self):
        """F73 — with the fork's live-io sidecar the long-request
        heartbeat says HOW MUCH the agent is thinking (real chars from
        the runtime's own accumulator) and quotes its last thought —
        reading a silent 28-min pure-reasoning request, not just
        counting requests."""
        self._spawn_entry(label="prover")
        self.ta.poll_spawns()
        a = self.ta.agents[0]
        a.aid = "agent_live1"
        a.last_beat = 0          # the heartbeat window is due
        ta = agent_live.TurnAgents(CFG, SID, "title", float(T0), deps=dict(
            self.deps,
            activity=lambda aid: {},
            pipe_age=lambda: None,
            live=lambda aid: {"reasoningChars": 4200, "textChars": 0,
                              "reasoningTail": "first line\nso the <invariant> holds"}))
        ta.agents.append(a)
        ta.heartbeat()
        beat = self.tr.edits[-1][1]
        self.assertIn("in-flight ~4k chars", beat)
        self.assertIn("«so the &lt;invariant&gt; holds»", beat)

    def test_file_assigns_and_updates_the_index(self):
        self._spawn_entry()
        self.ta.poll_spawns()
        recs = [_rec((T0 + 10) * 1000.0, reasoning="x" * 10,
                     text="step one", tools=[("Bash", {"command": "ls"})]),
                _rec((T0 + 20) * 1000.0, reasoning="y" * 10,
                     text="step two", tools=[("Read", {"path": "a.py"})]),
                _rec((T0 + 30) * 1000.0, reason="stop",
                     text="All claims verified.")]
        self._agent_file("model-io-sess_subagent_agent_85205035-3294-4876-9862-5517f3511434.jsonl", recs)
        self.ta.assign_files()
        self.assertEqual(self.ta.agents[0].path.endswith("5517f3511434.jsonl"),
                         True)
        self.ta.poll_agents()
        a = self.ta.agents[0]
        self.assertTrue(a.done)
        # F65: the task thread saw ONLY the birth status line — no boxes
        self.assertEqual(len(self.tr.sent), 1)
        self.assertIn("/subagents", self.tr.sent[0][1])
        # the status line closes with the result
        self.assertIn("done", self.tr.edits[-1][1])
        # the index carries state + path (the view resolves the file)
        idx = agent_live.read_index(SID)
        entry = idx["agents"][0]
        self.assertEqual(entry["state"], "done")
        self.assertTrue(entry["path"].endswith("5517f3511434.jsonl"))

    def test_previous_turn_agent_file_is_ignored(self):
        self._agent_file("model-io-sess_subagent_agent_00000000-0000-4000-8000-000000000old.jsonl",
                         [_rec((T0 - 3600) * 1000.0, text="old")])
        self.ta.assign_files()
        self.assertEqual(self.ta.agents, [])

    def test_file_without_spawn_registers_implicit_agent(self):
        self._agent_file("model-io-sess_subagent_agent_11111111-1111-4111-8111-111111111new.jsonl",
                         [_rec((T0 + 10) * 1000.0, text="hi")])
        self.ta.assign_files()
        self.assertEqual(len(self.ta.agents), 1)

    def test_summary_no_birth_no_boxes_only_the_done_line(self):
        self.ta.summary = True
        self._spawn_entry()
        self.ta.poll_spawns()
        # F64: even in summary the ONE status line shows (it is the
        # "subagent running" notice); what summary drops is the INSIDE
        self.assertEqual(len(self.tr.sent), 1)
        recs = [_rec((T0 + 10) * 1000.0, reasoning="r" * 10, text="working",
                     tools=[("Bash", {"command": "ls"})]),
                _rec((T0 + 20) * 1000.0, reason="stop", text="Done: all good.")]
        self._agent_file("model-io-sess_subagent_agent_22222222-2222-4222-8222-222222222sum.jsonl", recs)
        self.ta.assign_files()
        self.ta.poll_agents()
        self.assertEqual(self.ta.agents[0].boxes, [])  # nothing sealed
        # the status line EDITED to done (no new messages, no boxes)
        self.assertEqual(len(self.tr.sent), 1)
        self.assertIn("done", self.tr.edits[-1][1])
        self.assertIn("Done: all good.", self.tr.edits[-1][1])

    def test_a_pass_lands_as_one_collapsed_bundle(self):
        """F70 — a pass's boxes NEVER travel as messages (26/10: a
        28-box request arrived as 28 messages — spam): ONE bundle with
        ONE «N cots» button (agx:) that unfolds the per-box keyboard;
        the cache keeps the boxes for the ag: taps. Verbose included —
        collapsed is the agent contract now (Damian's answer)."""
        name = "model-io-sess_subagent_agent_33333333-3333-4333-8333-333333333ver.jsonl"
        self._agent_file(name, [])
        p = agent_live._agent_files()[0]
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "t",
                       "agents": [{"n": 1, "label": "open-box", "state": "running",
                                   "path": p, "req_n": 0, "last": "", "prompt": ""}]}, f)
        box = {"appended": False}

        def wait(paths, t):
            if not box["appended"]:
                box["appended"] = True
                with open(p, "a") as f:
                    f.write(json.dumps(_rec((T0 + 10) * 1000.0, reason="stop",
                                            reasoning="deep",
                                            text="the final answer")) + "\n")
        sent = []
        deps = {"send": lambda cfg, text, loud=None, markup=None, a_outbox=True:
                (sent.append((text, markup)), 9001)[1],
                "read_new": _read_file, "now": lambda: T0 + 60,
                "sleep": lambda s: None, "wait": wait, "states": lambda: {},
                "activity": lambda aid: {}, "pipe_age": lambda: None}
        agent_live.view_agent(dict(CFG, verbosity="verbose"), SID, 1,
                              deps=deps)
        texts = [t for t, _ in sent]
        self.assertIn("Watching Agent 1", texts[0])       # the 🎯 header
        bundle = next((t, kb) for t, kb in sent if kb and "landed" in t)
        self.assertIn("1 cots", bundle[0])
        btn = bundle[1]["inline_keyboard"][0][0]
        self.assertIn("cots", btn["text"])
        # the unfold button: the view's own t0 (now injected: T0 + 60)
        self.assertEqual(btn["callback_data"], "agx:%s:%d:1:1:1" % (SID, T0 + 60))
        self.assertTrue(any("final answer" in t for t in texts))
        self.assertTrue(any("back to the task" in t for t in texts))

    def test_cut_all_marks_the_cut_and_the_index(self):
        self._spawn_entry()
        self.ta.poll_spawns()
        self._agent_file("model-io-sess_subagent_agent_44444444-4444-4444-8444-444444444cut.jsonl",
                         [_rec((T0 + 10) * 1000.0, reasoning="half-done work")])
        self.ta.assign_files()
        self.ta.poll_agents()
        self.ta.cut_all("turn ended")
        self.assertIn("cut", self.tr.edits[-1][1])
        idx = agent_live.read_index(SID)
        self.assertEqual(idx["agents"][0]["state"], "turn ended")

    def test_cache_written_per_agent_with_phs(self):
        self._spawn_entry()
        self.ta.poll_spawns()
        self._agent_file("model-io-sess_subagent_agent_55555555-5555-4555-8555-555555555wri.jsonl",
                         [_rec((T0 + 10) * 1000.0, reason="stop", text="ok")])
        self.ta.assign_files()
        self.ta.poll_agents()
        with open(agent_live.agent_cache_path(SID, T0, 1)) as f:
            d = json.load(f)
        self.assertEqual(d["sid"], SID)
        self.assertEqual(d["agent"], 1)
        self.assertEqual(len(d["phs"]), len(d["boxes"]))
        # F64: the watcher seals to the CACHE — the phs stay None until
        # /subagents materializes the boxes (the view owns its messages)
        self.assertTrue(all(p is None for p in d["phs"]))


class TestTapPlumbing(unittest.TestCase):
    """ag:/agr: callbacks, the in-place expansion and the vocabulary
    rewrite of the CoT-shaped rows."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_callback_translation(self):
        self.assertEqual(cb.callback_text(
            "ag:%s:%d:1:2" % (SID, T0)), "/agb %s %d 1 2" % (SID, T0))
        self.assertEqual(cb.callback_text(
            "agr:%s:%d:1:2" % (SID, T0)), "/agr %s %d 1 2" % (SID, T0))

    def test_ag_rows_rewrites_the_cot_vocabulary(self):
        rows = reasoning.part_buttons(SID, T0, 3, 2, 1) \
            + reasoning.collapse_buttons(SID, T0, 3)
        out = agent_live._ag_rows(rows, 7)
        data = [b["callback_data"] for row in out for b in row]
        self.assertEqual(data,
                         ["ag:%s:%d:7:3:1" % (SID, T0),
                          "ag:%s:%d:7:3:2" % (SID, T0),
                          "agr:%s:%d:7:3" % (SID, T0)])

    def test_agb_expands_in_place_on_its_own_message(self):
        support.patch(self, hi.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((ph, text, markup)), True)[1])
        self.edits = []
        with open(agent_live.agent_cache_path(SID, T0, 2), "w") as f:
            json.dump({"sid": SID, "t0": T0, "agent": 2, "perbox": True,
                       "title": "t",
                       "boxes": ["work of one", "work of two"],
                       "labels": ["Agent 2.1", "Agent 2.2"],
                       "phs": [7001, 7002]}, f)
        out = hi._cmd_agb(CFG, "%s %d 2 2 ph=7002" % (SID, T0))
        self.assertIsNone(out)  # the edit WAS the answer
        ph, text, kb = self.edits[0]
        self.assertEqual(ph, 7002)
        self.assertIn("work of two", text)
        rows = kb["inline_keyboard"]
        self.assertEqual(rows[-1][-1]["callback_data"],
                         "agr:%s:%d:2:2" % (SID, T0))

    def test_agb_falls_to_the_reader_on_a_foreign_message(self):
        sent = []
        support.patch(self, hi.uhttp, "_live_edit", lambda *a, **k: False)
        support.patch(self, hi, "_reader_show",
                      lambda cfg, text, kb: (sent.append(text), 9001)[1])
        with open(agent_live.agent_cache_path(SID, T0, 1), "w") as f:
            json.dump({"sid": SID, "t0": T0, "agent": 1, "perbox": True,
                       "title": "t", "boxes": ["only work"],
                       "labels": ["Agent 1.1"], "phs": [7010]}, f)
        out = hi._cmd_agb(CFG, "%s %d 1 1 ph=7999" % (SID, T0))
        self.assertIsNone(out)
        self.assertIn("only work", sent[0])

    def test_agr_folds_back_to_the_frozen_line(self):
        self.edits = []
        support.patch(self, hi.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (self.edits.append((ph, text, markup)), True)[1])
        with open(agent_live.agent_cache_path(SID, T0, 1), "w") as f:
            json.dump({"sid": SID, "t0": T0, "agent": 1, "perbox": True,
                       "title": "t", "boxes": ["folded work"],
                       "labels": ["Agent 1.1"], "phs": [7010]}, f)
        out = hi._cmd_agr(CFG, "%s %d 1 1 ph=7010" % (SID, T0))
        self.assertIsNone(out)
        ph, text, kb = self.edits[0]
        self.assertIn("Agent 1.1", text)
        self.assertTrue(kb["inline_keyboard"][0][0]["callback_data"]
                        .startswith("ag:"))


class TestRealNameAndLiveness(unittest.TestCase):
    """25/10 regression pins: the REAL agent file name (underscore —
    the first cut matched a hyphen and the eri turn went mute), the
    stale pid-file rule and the startup grace of the loop."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_real_agent_file_name_is_discovered(self):
        d = os.path.dirname(config.rollout_path("x"))
        os.makedirs(d, exist_ok=True)
        real = os.path.join(
            d, "model-io-sess_subagent_agent_3cc56ee1-77ec-4c97-9ca7-57185fec3ba7.jsonl")
        open(real, "w").close()
        self.assertIn(real, agent_live._agent_files())

    def test_pid_file_heartbeat_decides_single_flight(self):
        """F65c — freshness replaced the marker-age rule: the watcher
        LINGERS past its turn, so an old-but-FRESH pid must still block
        the spawn (the marker rule would double-spawn on the very next
        turn of a session with agents in flight); silence (no touches)
        frees it."""
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(agent_live.watcher_pid_path(SID), "w") as f:
            f.write("4242")
        old = time.time() - 600
        os.utime(agent_live.watcher_pid_path(SID), (old, old))
        self.assertFalse(agent_live.watcher_up(SID, proc_alive=lambda p: True))
        with open(config.marker_path(SID), "w") as f:
            json.dump({"token": "new-turn"}, f)   # a NEWER marker must not
        self.assertFalse(agent_live.watcher_up(SID, proc_alive=lambda p: True))
        os.utime(agent_live.watcher_pid_path(SID), None)  # touched NOW
        self.assertTrue(agent_live.watcher_up(SID, proc_alive=lambda p: True))

    def test_loop_survives_the_startup_grace_then_ends(self):
        support.patch(self, agent_live.config, "code_changed", lambda: False)
        t = [1000.0]

        def now():
            t[0] += 1.0
            return t[0]
        waits = []
        deps = {"now": now, "running": lambda: {},
                "states": lambda: {},
                "wait": lambda paths, timeout: waits.append(timeout),
                "send": lambda *a, **k: 1, "edit": lambda *a, **k: True,
                "delete": lambda *a, **k: None, "read_new": _read_file}
        agent_live.watch_agents(CFG, SID, "t", 1000.0, "/ws", deps=deps)
        # inside the grace the loop kept going (waits happened); past it,
        # with no running turn, no marker and nothing alive, it ended
        self.assertGreaterEqual(len(waits), 1)


def _rec_linked(ms, cwd, prompt, text="hi", reason="stop"):
    """A first record with the REAL linkage fields (25/10, eri): the
    request's sdkMessages carry the parent's cwd and, as the last user
    message, the Task prompt verbatim."""
    r = _rec(ms, reason=reason, text=text)
    r["request"] = {"sdkMessages": [
        {"role": "system", "content": "You work in %s today." % cwd},
        {"role": "user", "content": prompt}]}
    return r


class TestLinkage(unittest.TestCase):
    """Parent → agent linkage: the workspace filters foreign agents, the
    prompt resolves which spawn a file belongs to (parallel spawns)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.tr = _Transport()
        self.deps = {"send": self.tr.send, "edit": self.tr.edit,
                     "delete": self.tr.delete, "read_new": _read_file,
                     "now": lambda: T0 + 60, "ws": "/Users/x/Projects/eri"}
        self.ta = agent_live.TurnAgents(CFG, SID, "eri", float(T0),
                                        deps=dict(self.deps))
        self.d = os.path.dirname(config.rollout_path("x"))
        os.makedirs(self.d, exist_ok=True)

    def _file(self, name, rec):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(json.dumps(rec) + "\n")
        return p

    def _spawn(self, label, prompt):
        with open(agent_live.spawn_log_path(SID), "a") as f:
            f.write(json.dumps({"ts": T0 + 5, "label": label,
                                "prompt": prompt[:200]}) + "\n")

    def test_foreign_workspace_agent_is_ignored(self):
        self._file("model-io-sess_subagent_agent_aaaaaaaa-0000-4000-8000-000000000001.jsonl",
                   _rec_linked((T0 + 10) * 1000.0, "/Users/x/Projects/bot",
                               "do bot things"))
        self.ta.assign_files()
        self.assertEqual(self.ta.agents, [])

    def test_prompt_resolves_parallel_spawns_regardless_of_file_order(self):
        self._spawn("Explore", "First task: read the parser")
        self._spawn("Verify", "Second task: check the claims")
        self.ta.poll_spawns()
        # the SECOND spawn's file lands first
        self._file("model-io-sess_subagent_agent_bbbbbbbb-0000-4000-8000-000000000002.jsonl",
                   _rec_linked((T0 + 10) * 1000.0, "/Users/x/Projects/eri",
                               "Second task: check the claims carefully"))
        self._file("model-io-sess_subagent_agent_cccccccc-0000-4000-8000-000000000003.jsonl",
                   _rec_linked((T0 + 11) * 1000.0, "/Users/x/Projects/eri",
                               "First task: read the parser now"))
        self.ta.assign_files()
        by_label = {a.label: os.path.basename(a.path or "") for a in self.ta.agents}
        self.assertIn("000000000002", by_label["Verify"])
        self.assertIn("000000000003", by_label["Explore"])

    def test_implicit_agent_is_labelled_by_its_own_prompt(self):
        self._file("model-io-sess_subagent_agent_dddddddd-0000-4000-8000-000000000004.jsonl",
                   _rec_linked((T0 + 10) * 1000.0, "/Users/x/Projects/eri",
                               "You are a research mathematician in topology"))
        self.ta.assign_files()
        self.assertEqual(len(self.ta.agents), 1)
        self.assertIn("research mathematician", self.tr.sent[0][1])


class TestSubagentsView(unittest.TestCase):
    """/subagents (F64): the list with descriptive-title buttons, the
    pick → run view, and the /status section."""

    def setUp(self):
        self.zt = support.fresh(self)

    def _index(self, agents, t0=T0, sid=SID, title="eri"):
        with open(agent_live.index_path(sid), "w") as f:
            json.dump({"sid": sid, "t0": t0, "title": title,
                       "agents": agents}, f)

    def test_list_with_one_button_per_agent(self):
        self._index([
            {"n": 1, "label": "Explore topology", "state": "running",
             "req_n": 3, "boxes": 2, "last": "Bash ls", "prompt": "p"},
            {"n": 2, "label": "Verify claims", "state": "done",
             "req_n": 5, "boxes": 4, "last": "done", "prompt": "p"}])
        text, kb = agent_live.subagents_list(SID, "eri")
        self.assertIn("Explore topology", text)
        self.assertIn("🟢", text)
        self.assertIn("✅", text)
        rows = kb["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"],
                         "sa:%s:%d:1" % (SID, T0))

    def test_list_empty_is_a_clean_reply(self):
        text, kb = agent_live.subagents_list(SID, "eri")
        self.assertIsNone(kb)
        self.assertIn("No subagents", text)

    def test_show_without_cache_uses_the_index(self):
        self._index([{"n": 1, "label": "Only spawned", "state": "running",
                      "req_n": 0, "boxes": 0, "last": "waiting",
                      "prompt": "p"}])
        out = agent_live.subagent_show(CFG, SID, T0, 1, send=lambda *a, **k: 1)
        self.assertIn("Only spawned", out)
        self.assertIn("no boxes yet", out)

    def test_status_lines_running_agents(self):
        self._index([
            {"n": 1, "label": "Explore topology", "state": "running",
             "req_n": 2, "boxes": 1, "last": "Read src/x.py", "prompt": "p"},
            {"n": 2, "label": "Verify", "state": "done",
             "req_n": 4, "boxes": 3, "last": "", "prompt": "p"}])
        lines = agent_live.status_lines(SID)
        self.assertTrue(any("1 running" in ln for ln in lines))
        self.assertTrue(any("Explore topology" in ln for ln in lines))

    def test_status_lines_none_without_index(self):
        self.assertEqual(agent_live.status_lines(None), [])


class TestPushGateAndOtherSessions(unittest.TestCase):
    """F64b — the F15 gate read live on every push, and /subagents
    listing the OTHER sessions' agents (the Mac's)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.tr = _Transport()
        self.allow = [False]
        self.deps = {"send": self.tr.send, "edit": self.tr.edit,
                     "delete": self.tr.delete, "read_new": _read_file,
                     "now": lambda: T0 + 60,
                     "may_push": lambda: self.allow[0]}
        self.ta = agent_live.TurnAgents(CFG, SID, "mac task", float(T0),
                                        deps=dict(self.deps))

    def _spawn(self, label="Explore"):
        with open(agent_live.spawn_log_path(SID), "a") as f:
            f.write(json.dumps({"ts": T0 + 5, "label": label,
                                "prompt": "p"}) + "\n")

    def test_quiet_watcher_indexes_without_pushing(self):
        self._spawn()
        self.ta.poll_spawns()
        self.assertEqual(self.tr.sent, [])        # nothing in the chat
        idx = agent_live.read_index(SID)
        self.assertEqual(idx["agents"][0]["label"], "Explore")  # but indexed
        # the session becomes the active task mid-turn → the next agent's
        # line pushes (F15: a /task switch starts the stream)
        self.allow[0] = True
        self._spawn("Verify")
        self.ta.poll_spawns()
        self.assertEqual(len(self.tr.sent), 1)
        self.assertIn("Verify", self.tr.sent[0][1])

    def test_list_includes_other_sessions_with_running_agents(self):
        other = "session_other0000000000000000000000000001"
        with open(agent_live.index_path(other), "w") as f:
            json.dump({"sid": other, "t0": T0, "title": "eri",
                       "updated": time.time(),
                       "agents": [{"n": 1, "label": "Topology prover",
                                   "state": "running", "req_n": 2,
                                   "boxes": 1, "last": "x", "prompt": "p"}]}, f)
        text, kb = agent_live.subagents_list(SID, "phone task")
        self.assertIn("No subagents in «phone task»", text)
        self.assertIn("«eri»", text)
        self.assertIn("other session", text)
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"],
                         "sa:%s:%d:1" % (other, T0))
        lines = agent_live.status_lines(SID)
        self.assertTrue(any("other sessions" in ln and "«eri» 1" in ln
                            for ln in lines))

    def test_stale_other_session_is_not_listed(self):
        other = "session_other0000000000000000000000000002"
        with open(agent_live.index_path(other), "w") as f:
            json.dump({"sid": other, "t0": T0, "title": "old",
                       "updated": time.time() - 7200,
                       "agents": [{"n": 1, "label": "done long ago",
                                   "state": "done", "req_n": 2,
                                   "boxes": 1, "last": "", "prompt": "p"}]}, f)
        self.assertEqual(agent_live.indexes_live(exclude_sid=SID), [])


class TestAgentView(unittest.TestCase):
    """F65 — the view in the task's format; F68 — LIVE FROM THE PICK:
    no history replay (Damian: «lo quiero desde el momento que lo  # noqa: spanish — Damian's quoted request
    seleccioné»), a done agent answers at once, the log closes the rest;  # noqa: spanish — continuation
    and Damian's exit rule (/task sweeps the pid-file)."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.d = os.path.dirname(config.rollout_path("x"))
        os.makedirs(self.d, exist_ok=True)

    def _setup(self, sid, recs, name, aid=""):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        with open(agent_live.index_path(sid), "w") as f:
            json.dump({"sid": sid, "t0": T0, "title": "eri",
                       "agents": [{"n": 1, "label": "prover", "state": "running",
                                   "path": p, "req_n": 0, "last": "",
                                   "prompt": "", "aid": aid}]}, f)
        return p

    def _view(self, sid, recs, name, cfg=None, wait=None, states=None, aid=""):
        self._setup(sid, recs, name, aid=aid)
        sent = []
        deps = {"send": lambda cfg, text, loud=None, markup=None, a_outbox=True:
                (sent.append((text, markup)), 9100)[1],
                "read_new": _read_file, "now": lambda: T0 + 60,
                "sleep": lambda s: None,
                "wait": wait or (lambda p, t: agent_live.stop_views()),
                "states": states or (lambda: {}),
                "running": lambda: {sid: T0}}
        n = agent_live.view_agent(cfg or CFG, sid, 1, deps=deps)
        return sent, n

    OLD = [_rec((T0 + 10) * 1000.0, reasoning="thinking about ls",
                text="step one", tools=[("Bash", {"command": "ls"})])]

    def test_done_at_pick_answers_at_once_without_history(self):
        sent, n = self._view(SID, self.OLD + [
            _rec((T0 + 20) * 1000.0, reason="stop", text="All done: 3 files.")],
            "model-io-sess_subagent_agent_66666666-6666-4666-8666-666666666vie.jsonl")
        texts = [t for t, _ in sent]
        self.assertTrue(any("already finished" in t for t in texts))
        self.assertTrue(any("final answer" in t and "All done" in t
                            for t in texts))
        self.assertFalse(any("step one" in t for t in texts))   # no history
        self.assertFalse(any("ls" in t for t in texts))         # no old tool box
        self.assertFalse(any("thinking about" in t for t in texts))

    def test_live_from_the_pick_only(self):
        """The 26/10 report: dozens of CoT boxes on pick — the whole run
        replayed. Now the pre-pick records never travel; what the agent
        does AFTER the pick does."""
        name = "model-io-sess_subagent_agent_77777777-7777-4777-8777-777777777vie.jsonl"
        p = os.path.join(self.d, name)
        box = {"appended": False}

        def wait(paths, timeout):
            if not box["appended"]:
                box["appended"] = True
                with open(p, "a") as f:
                    f.write(json.dumps(_rec(
                        (T0 + 30) * 1000.0, text="fresh step",
                        tools=[("Read", {"file_path": "/x/y.py"})])) + "\n")
                    f.write(json.dumps(_rec(
                        (T0 + 40) * 1000.0, reason="stop",
                        text="Fresh result.")) + "\n")
            else:
                agent_live.stop_views()
        sent, n = self._view(SID, self.OLD, name, wait=wait)
        texts = [t for t, _ in sent]
        self.assertTrue(any("Watching Agent 1" in t and "live from this moment" in t
                            for t in texts))
        self.assertFalse(any("step one" in t for t in texts))    # pre-pick: out
        self.assertFalse(any("ls" in t for t in texts))
        self.assertTrue(any("fresh step" in t for t in texts))   # post-pick: in
        self.assertTrue(any("/x/y.py" in t for t in texts))
        self.assertTrue(any("final answer" in t and "Fresh result" in t
                            for t in texts))
        self.assertTrue(any("back to the task" in t for t in texts))

    def test_log_truth_closes_a_view_the_file_never_will(self):
        """A failed/stopped agent writes no stop record: the F65c log
        truth (by the row's aid) closes the view with the reason."""
        sent, n = self._view(
            SID, self.OLD,
            "model-io-sess_subagent_agent_88888888-8888-4888-8888-888888888vie.jsonl",
            wait=lambda p, t: None, aid="agent_x",
            states=lambda: {"agent_x": {"state": "failed", "bg": True,
                                        "error": "user concurrency limit exceeded"}})
        texts = [t for t, _ in sent]
        self.assertTrue(any("Agent 1 failed" in t and "concurrency" in t
                            for t in texts))

    def test_the_cot_streams_live_from_the_messages(self):
        """F70b — resume + session/messages serves the agent's OWN
        session (verified live 26/10: the store answers sess_subagent_*
        once resumed — my earlier cold call failed only for lacking the
        resume). The filling box is born at its start, edited while it
        grows, frozen with its ag: button at the seal: the CoT streams
        LIVE mid-request, no fork needed. The record does NOT
        double-seal what the ledger sealed; the file keeps its tools,
        narration and final answer."""
        from zcode_bot import reasoning as zr
        CAP = zr.COT_BOX_CAP
        uuid = "5f5f5f5f-555f-455f-855f-5555555555f5"
        p = self._setup(
            SID,
            [_rec((T0 + 5) * 1000.0, text="first step",
                  tools=[("Read", {"file_path": "/a.py"})])],
            "model-io-sess_subagent_agent_%s.jsonl" % uuid, aid="agent_" + uuid)

        def _m(role, *parts):
            return {"info": {"role": role}, "parts": list(parts)}

        def _rr(t):
            return {"type": "reasoning", "text": t}

        part1 = "x" * int(CAP * 0.6)
        part2 = "x" * (CAP + 250)          # seals box 1.1, fills 1.2
        stages = [
            [_m("user", {"type": "text", "text": "go"}),
             _m("assistant", _rr(part1))],
            [_m("user", {"type": "text", "text": "go"}),
             _m("assistant", _rr(part2))],
            [_m("user", {"type": "text", "text": "go"}),
             _m("assistant", _rr(part2), {"type": "text", "text": "done thinking"})],
        ]
        box = {"i": 0, "file_done": False}

        def agent_msgs():
            i = min(box["i"], len(stages) - 1)
            box["i"] += 1
            return stages[i]

        def wait(paths, t):
            if box["i"] >= len(stages) and not box["file_done"]:
                box["file_done"] = True
                with open(p, "a") as f:
                    f.write(json.dumps(_rec(
                        (T0 + 20) * 1000.0, reasoning=part2,
                        tools=[("Read", {"file_path": "/a.py"})])) + "\n")
                    f.write(json.dumps(_rec(
                        (T0 + 30) * 1000.0, reason="stop",
                        text="Final.")) + "\n")
        sent, edits = [], []
        deps = {"send": lambda cfg, text, loud=None, markup=None, a_outbox=True:
                (sent.append((text, markup)), 9100 + len(sent))[1],
                "edit": lambda cfg, ph, text, markup=None:
                (edits.append((ph, text, markup)), True)[1],
                "read_new": _read_file, "now": lambda: T0 + 60,
                "sleep": lambda s: None, "wait": wait,
                "states": lambda: {}, "agent_msgs": agent_msgs,
                "activity": lambda aid: {}, "pipe_age": lambda: None,
                "running": lambda: {}}
        agent_live.view_agent(CFG, SID, 1, deps=deps)
        # the LIVE flow: born filling, edited, frozen with its button
        self.assertTrue(any("filling CoT 1.1" in t for t, _ in sent))
        self.assertTrue(any("filling CoT 1.2" in t for t, _ in sent))
        frozen = [(t, kb) for _, t, kb in edits
                  if kb and "CoT" in t and "<b>" in t]
        self.assertTrue(frozen, edits)
        cbs = [b["callback_data"] for _, kb in frozen
               for row in (kb or {}).get("inline_keyboard", []) for b in row]
        self.assertIn("ag:%s:%d:1:1" % (SID, T0 + 60), cbs)
        self.assertIn("ag:%s:%d:1:2" % (SID, T0 + 60), cbs)
        # the record did NOT double-seal: exactly 2 boxes in the cache
        with open(agent_live.agent_cache_path(SID, T0 + 60, 1)) as f:
            d = json.load(f)
        self.assertEqual(len(d["boxes"]), 2)
        self.assertTrue(all(d["phs"]))
        # the file's cargo still traveled: tool + final + close
        self.assertTrue(any("/a.py" in t for t, _ in sent))
        self.assertTrue(any("final answer" in t and "Final." in t
                            for t, _ in sent))
        self.assertTrue(any("back to the task" in t for t, _ in sent))
        # and no per-box bundle: the CoT was LIVE, not collapsed
        self.assertFalse([t for t, _ in sent if "landed" in t])

    def test_view_without_a_file_is_a_clean_reply(self):
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "t",
                       "agents": [{"n": 9, "label": "old", "state": "done",
                                   "path": "", "req_n": 0, "last": "",
                                   "prompt": "gone prompt"}]}, f)
        sent, n = self._view(SID, [], "model-io-sess_subagent_agent_none.jsonl",
                             cfg=CFG)
        # the file never existed → nothing matched: the view says so and ends
        # (index path empty and no prompt-file match for agent 9)

    def test_task_pick_sweeps_the_views(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(agent_live.view_pid_path(SID), "w") as f:
            f.write("1234")
        agent_live.stop_views()
        self.assertFalse(os.path.exists(agent_live.view_pid_path(SID)))

    def test_pick_mid_request_beats_until_the_log_closes(self):
        """F69 — picked mid-request (nothing on disk until the request
        completes — verified 26/10: session/messages is empty for
        subagent sessions, the rollout only grows at completion): ONE
        live line at once, edited by the minute, closed with the
        runtime's word."""
        uuid = "ffffffff-0000-4000-8000-0000000000f1"
        self._setup(SID, self.OLD,                         # pass 1 done
                    "model-io-sess_subagent_agent_%s.jsonl" % uuid,
                    aid="agent_" + uuid)
        box = [float(T0)]

        def now():
            box[0] += 3.0
            return box[0]
        sent, edits = [], []

        def states():
            st = "running" if box[0] < T0 + 200 else "failed"
            return {"agent_" + uuid: {
                "state": st, "bg": True,
                "error": "user concurrency limit exceeded" if st == "failed"
                else ""}}
        deps = {"send": lambda cfg, text, loud=None, markup=None, a_outbox=True:
                (sent.append(text), 9100 + len(sent))[1],
                "edit": lambda cfg, ph, text, markup=None:
                (edits.append((ph, text)), True)[1],
                "read_new": _read_file, "now": now, "sleep": lambda s: None,
                "wait": lambda p, t: None, "states": states,
                "running": lambda: {}}
        agent_live.view_agent(CFG, SID, 1, deps=deps)
        beats = [t for t in sent if "pass 2" in t and "thinking" in t]
        self.assertEqual(len(beats), 1)              # born once, at the pick
        self.assertTrue(any("pass 2" in t and "min" in t
                            for _, t in edits))      # re-edited with activity
        self.assertTrue(any("failed" in t for _, t in edits))  # closed w/ the log
        self.assertTrue(any("Agent 1 failed" in t for t in sent))
        self.assertTrue(any("concurrency" in t for t in sent))

    def test_agx_unfolds_the_bundles_keyboard_in_place(self):
        """F70 — the 📚 button: the bundle message's keyboard becomes the
        per-box buttons (ag:) IN PLACE; the boxes live in the cache."""
        import zcode_bot.reasoning as zr
        from zcode_bot.utils import http as zu
        with open(agent_live.agent_cache_path(SID, T0, 1), "w") as f:
            json.dump({"sid": SID, "t0": T0, "agent": 1,
                       "boxes": ["b1", "b2"], "labels": ["A1 CoT 4.1",
                                                         "A1 CoT 4.2"],
                       "phs": [90, 90], "perbox": True}, f)
        edits = []
        support.patch(self, zu, "_live_edit",
                      lambda cfg, ph, text, markup=None:
                      (edits.append((ph, text, markup)), True)[1])
        out = hi._cmd_agx(CFG, "%s %d 1 1 2 ph=90" % (SID, T0))
        self.assertIsNone(out)
        ph, text, kb = edits[0]
        self.assertEqual(ph, 90)
        self.assertIn("2 cots", text)
        datas = [b["callback_data"] for row in kb["inline_keyboard"]
                 for b in row]
        self.assertEqual(datas, ["ag:%s:%d:1:1" % (SID, T0),
                                 "ag:%s:%d:1:2" % (SID, T0)])

    def test_ended_rows_prune_after_the_keep_window(self):
        """F70 — Damian: «la lista se limpie... muchísimos botones con  # noqa: spanish — Damian's quoted request
        subagentes que ya terminaron»: an ended row stays DONE_KEEP_S  # noqa: spanish — continuation
        (tappable, /subagents n works), then the list cleans itself."""
        now = time.time()
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "eri",
                       "updated": now - 3600, "agents": [
                {"n": 1, "label": "old done", "state": "done",
                 "req_n": 1, "aid": "agent_a", "prompt": "p",
                 "done_at": now - agent_live.DONE_KEEP_S - 300},
                {"n": 2, "label": "fresh done", "state": "done",
                 "req_n": 1, "aid": "agent_b", "prompt": "p",
                 "done_at": now - 60},
                {"n": 3, "label": "still runs", "state": "running",
                 "req_n": 1, "aid": "agent_c", "prompt": "p"}]}, f)
        idx = agent_live.reconciled_index(
            SID, states_fn=lambda: {})   # nothing to correct
        self.assertEqual([r["n"] for r in idx["agents"]], [2, 3])


class TestNoteSpawn(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def test_appends_the_label_for_the_watcher(self):
        agent_live.note_spawn(SID, "Task",
                              {"description": "Explore", "prompt": "p" * 500})
        with open(agent_live.spawn_log_path(SID)) as f:
            d = json.loads(f.readline())
        self.assertEqual(d["label"], "Explore")
        self.assertEqual(len(d["prompt"]), 200)  # preview, not the whole


UUID1 = "eeeeeeee-0000-4000-8000-0000000000e1"


class TestLogTruth(unittest.TestCase):
    """F65c — the runtime's own word (the CLI log, replayed by agentId)
    replaces time windows: liveness, adoption, seed and reconcile."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.d = os.path.dirname(config.rollout_path("x"))
        os.makedirs(self.d, exist_ok=True)

    def _file(self, name, rec):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(json.dumps(rec) + "\n")
        return p

    def test_states_replay_last_event_wins(self):
        evs = iter([
            (1000.0, SID, "subagent.background.started",
             {"context": {"agentId": "agent_a"}}),
            (1001.0, SID, "subagent.background.notification.enqueued",
             {"context": {"agentId": "agent_a"}}),     # noise: skipped
            (1002.0, SID, "subagent.spawned",
             {"context": {"agentId": "agent_b"}}),
            (1003.0, SID, "subagent.background.failed",
             {"context": {"agentId": "agent_a",
                          "errorMessage": "user concurrency limit exceeded"}}),
            (1004.0, SID, "subagent.completed",
             {"context": {"agentId": "agent_b"}}),
        ])
        st = agent_live.subagent_states(SID, iter_ev=lambda *a, **k: evs)
        self.assertEqual(st["agent_a"]["state"], "failed")
        self.assertEqual(st["agent_a"]["error"], "user concurrency limit exceeded")
        self.assertTrue(st["agent_a"]["bg"])
        self.assertEqual(st["agent_b"]["state"], "done")
        self.assertFalse(st["agent_b"]["bg"])

    def test_states_running_with_no_end_reads_as_lost(self):
        """F69 — a 2 h cutoff forgot agents still running (eri's 07:36
        topologists at 09:50); the window now covers both log files, and
        a running agent with no end for AGENT_MAX_LIFE_S is «lost» (the
        app was killed — no terminal will ever come)."""
        old = time.time() - agent_live.AGENT_MAX_LIFE_S - 600
        recent = time.time() - 60
        evs = iter([
            (old, SID, "subagent.background.started",
             {"context": {"agentId": "agent_old"}}),
            (recent, SID, "subagent.background.started",
             {"context": {"agentId": "agent_new"}}),
        ])
        st = agent_live.subagent_states(SID, iter_ev=lambda *a, **k: evs)
        self.assertEqual(st["agent_old"]["state"], "lost")
        self.assertIn("killed", st["agent_old"]["error"])
        self.assertEqual(st["agent_new"]["state"], "running")

    def test_agent_request_activity_reads_the_agents_own_events(self):
        """F70 — the agent's life signs: its model.* events under its OWN
        sess_subagent_* sessionId (iteration, network failures), with the
        counters reset per request in flight."""
        ses = agent_live.agent_session_of("agent_x")
        evs = iter([
            (1000.0, ses, "model.request.started",
             {"context": {"iteration": 0, "messageCount": 12}}),
            (1010.0, ses, "model.network.failed", {}),
            (1011.0, ses, "model.request.completed", {}),
            (1020.0, ses, "model.request.started",
             {"context": {"iteration": 3, "messageCount": 20}}),
        ])
        act = agent_live.agent_request_activity(
            "agent_x", now=1050.0, iter_ev=lambda *a, **k: evs)
        self.assertTrue(act["in_flight"])
        self.assertEqual(act["started"], 1020.0)
        self.assertEqual(act["iteration"], 3)
        self.assertEqual(act["net_fails"], 0)   # reset when the new one started

        evs2 = iter([(1000.0, ses, "model.request.started", {}),
                     (1010.0, ses, "model.request.completed", {})])
        act = agent_live.agent_request_activity(
            "agent_x", now=1050.0, iter_ev=lambda *a, **k: evs2)
        self.assertFalse(act["in_flight"])      # between requests

    def test_last_event_age_is_the_pipe_stamp(self):
        """F70 — the pipe stamp: how long ago the runtime last wrote an
        event (any session — a subagent's request counts: same pipe)."""
        recent = time.time() - 3
        evs = iter([(recent, SID, "turn.started", {})])
        age = agent_live.last_event_age(
            now=recent + 3, iter_ev=lambda *a, **k: evs)
        self.assertIsNotNone(age)
        self.assertLessEqual(age, 3.1)
        self.assertIsNone(agent_live.last_event_age(
            now=time.time() + 10, iter_ev=lambda *a, **k: iter([])))

    def test_alive_aid_adopts_an_old_file(self):
        """The 26/10 case: the agent's first completed request lands
        AFTER the turn ended — the ms window would call it a previous
        turn's agent; the log says it runs."""
        ta = agent_live.TurnAgents(
            CFG, SID, "eri", float(T0), deps={"now": lambda: T0 + 600})
        ta.states = {"agent_" + UUID1: {"state": "running", "bg": True}}
        self._file("model-io-sess_subagent_agent_%s.jsonl" % UUID1,
                   _rec_linked((T0 - 60) * 1000.0, "/Users/x/Projects/eri",
                               "Prove the claim with rigor"))
        ta.assign_files()
        self.assertEqual(len(ta.agents), 1)
        self.assertEqual(ta.agents[0].aid, "agent_" + UUID1)
        self.assertIsNotNone(ta.agents[0].path)

    def test_seed_resurrects_cut_rows_while_agents_run(self):
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "eri", "agents": [
                {"n": 1, "label": "Probador A", "state": "turn ended",
                 "req_n": 0, "prompt": "Prove the claim",
                 "last": "cut before its first request"},
                {"n": 2, "label": "Finished", "state": "done", "req_n": 3,
                 "prompt": "other", "last": "ok"}]}, f)
        ta = agent_live.TurnAgents(
            CFG, SID, "eri", float(T0 + 100),
            deps={"now": lambda: T0 + 100,
                  "states": lambda: {"agent_" + UUID1:
                                     {"state": "running", "bg": True}}})
        ta.seed()
        self.assertFalse(ta.agents[0].done)   # resurrected as PENDING
        self.assertEqual(ta.agents[0].state, "running")
        self.assertTrue(ta.agents[1].done)    # history stays history
        # its file lands, born BEFORE this watcher → adopted, prompt-matched
        self._file("model-io-sess_subagent_agent_%s.jsonl" % UUID1,
                   _rec_linked((T0 - 60) * 1000.0, "/Users/x/Projects/eri",
                               "Prove the claim with rigor"))
        ta.assign_files()
        self.assertIsNotNone(ta.agents[0].path)
        self.assertEqual(ta.agents[0].n, 1)   # the number survived the seed

    def test_seed_keeps_history_when_nothing_runs(self):
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "eri", "agents": [
                {"n": 1, "label": "Cut", "state": "turn ended",
                 "req_n": 0, "prompt": "p", "last": ""}]}, f)
        ta = agent_live.TurnAgents(CFG, SID, "eri", float(T0),
                                   deps={"now": lambda: T0,
                                         "states": lambda: {}})
        ta.seed()
        self.assertTrue(ta.agents[0].done)    # nothing runs: stays cut

    def test_new_turn_refresh_registers_only_newer_spawns(self):
        with open(agent_live.spawn_log_path(SID), "a") as f:
            f.write(json.dumps({"ts": T0 + 120, "label": "old",
                                "prompt": "old task"}) + "\n")
        ta = agent_live.TurnAgents(CFG, SID, "eri", float(T0 + 200),
                                   deps={"now": lambda: T0 + 200})
        ta.poll_spawns()
        self.assertEqual(ta.agents, [])       # older than this turn: skipped
        with open(agent_live.spawn_log_path(SID), "a") as f:
            f.write(json.dumps({"ts": T0 + 310, "label": "new",
                                "prompt": "new task"}) + "\n")
        ta.new_turn(T0 + 300)                 # a turn arrived while we live
        ta.poll_spawns()
        self.assertEqual([a.label for a in ta.agents], ["new"])
        self.assertEqual(ta.agents[0].t0, T0 + 300)  # frozen at ITS turn

    def test_reconcile_ends_by_log_terminal(self):
        p = self._file("model-io-sess_subagent_agent_%s.jsonl" % UUID1,
                       _rec_linked((T0 + 10) * 1000.0, "/Users/x/Projects/eri",
                                   "work", text="partial", reason="tool-calls"))
        ta = agent_live.TurnAgents(CFG, SID, "eri", float(T0),
                                   deps={"now": lambda: T0 + 60})
        a = ta._register("Refutador", "work", path=p, aid="agent_" + UUID1)
        a.pos = os.path.getsize(p)             # drained: no stop record
        ta.states = {"agent_" + UUID1: {
            "state": "failed", "bg": True, "ts": T0 + 50,
            "error": "user concurrency limit exceeded"}}
        ta.reconcile()
        self.assertTrue(a.done)
        self.assertEqual(a.state, "failed")
        self.assertIn("concurrency", agent_live.read_index(SID)["agents"][0]["last"])

    def test_agent_rows_carry_their_own_t0(self):
        _, kb = agent_live._agent_rows({
            "sid": SID, "t0": 900,
            "agents": [{"n": 1, "label": "A", "state": "running",
                        "req_n": 1, "t0": 100}]})
        self.assertEqual(kb[0][0]["callback_data"], "sa:%s:100:1" % SID)

    def test_reconciled_index_corrects_a_stale_running_row(self):
        """F69 — a watcher killed by a deploy leaves 🟢 rows frozen (26/10:
        agent 1 of eri finished 12 min after its watcher stepped down);
        the log's word corrects the row, persisted, every surface agrees."""
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "eri", "agents": [
                {"n": 1, "label": "A", "state": "running", "req_n": 2,
                 "aid": "agent_x", "prompt": "p", "last": "thinking…"}]}, f)
        idx = agent_live.reconciled_index(SID, states_fn=lambda: {
            "agent_x": {"state": "failed", "bg": True,
                        "error": "user concurrency limit exceeded"}})
        self.assertEqual(idx["agents"][0]["state"], "failed")
        self.assertIn("concurrency", idx["agents"][0]["last"])
        self.assertEqual(agent_live.read_index(SID)["agents"][0]["state"],
                         "failed")                       # persisted
        text, _ = agent_live.subagents_list(SID, "eri")  # no watcher needed
        self.assertIn("⏹", text)
        self.assertNotIn("🟢", text)

    def test_respawn_orphans_relaunches_only_what_the_log_runs(self):
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "eri", "ws": "/w",
                       "agents": [{"n": 1, "label": "A", "state": "running",
                                   "aid": "agent_x", "prompt": "p"}]}, f)
        spawned = []
        alive = lambda: {"agent_x": {"state": "running", "bg": True}}  # noqa: E731
        agent_live._respawn_at[0] = 0.0
        out = agent_live.respawn_orphans(
            {"agent_stream": True}, states_fn=alive, now=1000.0,
            spawn=lambda sid, title, ws: spawned.append((sid, title, ws)))
        self.assertEqual(out, [SID])
        self.assertEqual(spawned, [(SID, "eri", "/w")])
        # throttled: the next poller cycles inside the minute do nothing
        self.assertEqual(agent_live.respawn_orphans(
            {"agent_stream": True}, states_fn=alive, now=1030.0,
            spawn=lambda *a: spawned.append(a)), [])
        # a live watcher (fresh pid, alive process) holds the single-flight
        with open(agent_live.watcher_pid_path(SID), "w") as f:
            f.write(str(os.getpid()))
        self.assertEqual(agent_live.respawn_orphans(
            {"agent_stream": True}, states_fn=alive, now=1100.0,
            spawn=lambda *a: spawned.append(a)), [])
        os.remove(agent_live.watcher_pid_path(SID))
        # the log says it ended: nothing to watch (reconciled_index's job)
        self.assertEqual(agent_live.respawn_orphans(
            {"agent_stream": True}, now=1200.0,
            states_fn=lambda: {"agent_x": {"state": "done", "bg": True}},
            spawn=lambda *a: spawned.append(a)), [])
        self.assertEqual(len(spawned), 1)


class TestLinger(unittest.TestCase):
    """F65c — background agents outlive the parent turn: the watcher
    LINGERS past it instead of cutting them as dead (26/10, eri: the
    turn ended 16 s after spawning two pure-reasoning agents)."""

    def setUp(self):
        self.zt = support.fresh(self)
        support.patch(self, agent_live.config, "code_changed", lambda: False)
        self.d = os.path.dirname(config.rollout_path("x"))
        os.makedirs(self.d, exist_ok=True)
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(agent_live.spawn_log_path(SID), "a") as f:
            f.write(json.dumps({"ts": T0 + 5, "label": "Probador A",
                                "prompt": "Prove the claim"}) + "\n")

    def _loop(self, states_fn, now=None):
        box = [T0 - 1.0]

        def _now():
            box[0] += 1.0
            return box[0]
        now = now or _now
        waits = []
        deps = {"now": now, "running": lambda: {}, "states": states_fn,
                "wait": lambda p, timeout: waits.append(timeout),
                "send": lambda *a, **k: 1, "edit": lambda *a, **k: True,
                "delete": lambda *a, **k: None, "read_new": _read_file}
        agent_live.watch_agents(CFG, SID, "eri", float(T0), "/ws", deps=deps)
        return waits

    def test_linger_until_the_log_ends_the_agent(self):
        with open(os.path.join(
                self.d, "model-io-sess_subagent_agent_%s.jsonl" % UUID1),
                "w") as f:
            f.write(json.dumps(_rec((T0 + 30) * 1000.0, reason="stop",
                                    text="QED")) + "\n")
        box = [T0 - 1.0]

        def now():
            box[0] += 1.0
            return box[0]

        def states():
            # runs as background until t=T0+400, then the log says done
            state = "running" if box[0] < T0 + 400 else "done"
            return {"agent_" + UUID1: {"state": state, "bg": True}}
        waits = self._loop(states, now=now)
        idx = agent_live.read_index(SID)
        self.assertEqual(idx["agents"][0]["state"], "done")
        self.assertGreater(len(waits), 50)    # lingered well past the grace

    def test_foreground_spawn_cut_when_the_turn_dies(self):
        waits = self._loop(lambda: {})         # the log has nothing alive
        idx = agent_live.read_index(SID)
        self.assertEqual(idx["agents"][0]["state"], "turn ended")
        self.assertEqual(idx["agents"][0]["last"],
                         "cut before its first request")
        self.assertLess(len(waits), 30)        # no linger without the log


class TestViewMuteAndStepDown(unittest.TestCase):
    """F67 — picking a subagent means ONLY that subagent streams: the
    parent's lines mute while a view lives, and a /task pick stops even
    a mid-burst catch-up at the next message."""

    def setUp(self):
        self.zt = support.fresh(self)
        self.tr = _Transport()
        self.d = os.path.dirname(config.rollout_path("x"))
        os.makedirs(self.d, exist_ok=True)
        os.makedirs(config.STATE_DIR, exist_ok=True)

    def _view_pid(self, sid=SID, pid=1234):
        with open(agent_live.view_pid_path(sid), "w") as f:
            f.write(str(pid))
        return agent_live.view_pid_path(sid)

    def test_view_alive_is_freshness_not_existence(self):
        self.assertFalse(agent_live.view_alive(SID))     # no file
        p = self._view_pid()
        self.assertTrue(agent_live.view_alive(SID))
        old = time.time() - 600
        os.utime(p, (old, old))
        self.assertFalse(agent_live.view_alive(SID))     # killed: no mute

    def test_watcher_lines_mute_while_the_view_lives(self):
        from zcode_bot import core
        core.set_active_bot_session({"sid": SID, "title": "eri"})
        cfg = dict(CFG, only_active_task=True)
        ta = agent_live.TurnAgents(cfg, SID, "eri", float(T0),
                                   deps={"send": self.tr.send,
                                         "now": lambda: T0 + 30})
        self._view_pid()
        self.assertIsNone(ta._push_send("should not land"))
        os.remove(agent_live.view_pid_path(SID))
        self.assertIsNotNone(ta._push_send("resumed"))   # view gone → push

    def test_mid_burst_sweep_stops_the_view_at_once(self):
        """The 26/10 incident: the pid check ran BETWEEN rounds only, so
        a long burst kept landing after the user picked another task.
        Now EVERY send checks first — tool boxes and narration included
        (F68 shape: the burst is post-pick records, since history no
        longer travels)."""
        p = os.path.join(
            self.d, "model-io-sess_subagent_agent_%s.jsonl" % UUID1)
        with open(p, "w") as f:
            f.write(json.dumps(_rec((T0 + 5) * 1000.0, text="old")) + "\n")
        with open(agent_live.index_path(SID), "w") as f:
            json.dump({"sid": SID, "t0": T0, "title": "eri",
                       "agents": [{"n": 1, "label": "prover",
                                   "state": "running", "req_n": 0,
                                   "prompt": "p", "path": p}]}, f)
        box = {"appended": False}

        def wait(paths, timeout):
            if not box["appended"]:
                box["appended"] = True
                with open(p, "a") as f:
                    # ONE record, a burst of 4 messages: 3 tool boxes + narration
                    f.write(json.dumps(_rec(
                        (T0 + 30) * 1000.0, text="narrating",
                        tools=[("Bash", {"command": "a"}), ("Bash", {"command": "b"}),
                               ("Bash", {"command": "c"})])) + "\n")
            else:
                agent_live.stop_views()
        real_send = self.tr.send

        def sweeping_send(cfg, text, loud=None, markup=None):
            r = real_send(cfg, text, loud=loud, markup=markup)
            if len(self.tr.sent) == 2:        # the user picks another task
                os.remove(agent_live.view_pid_path(SID))   # mid-burst
            return r
        agent_live.view_agent(CFG, SID, 1,
                              deps={"send": sweeping_send,
                                    "edit": self.tr.edit,
                                    "delete": self.tr.delete,
                                    "read_new": _read_file,
                                    "now": lambda: T0 + 30, "wait": wait,
                                    "states": lambda: {},
                                    "sleep": lambda s: None})
        # header + the first tool box (whose send carried the sweep) — the
        # other two tool boxes and the narration never left
        self.assertEqual(len(self.tr.sent), 2)
        self.assertNotIn("narrating", " ".join(t for _, t, _m in self.tr.sent))

    def test_sa_pick_sweeps_every_previous_view(self):
        import subprocess
        self._view_pid(sid="sess_other")
        self._view_pid()
        seen = []
        support.patch(self, subprocess, "Popen",
                      lambda *a, **k: seen.append(a) or True)
        hi._cmd_sa(CFG, "%s %d 2" % (SID, T0))
        self.assertFalse(os.path.exists(agent_live.view_pid_path(SID)))
        self.assertFalse(os.path.exists(agent_live.view_pid_path("sess_other")))
        self.assertIn("--agent-view", seen[0][0])


if __name__ == "__main__":
    unittest.main()
