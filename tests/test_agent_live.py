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

    def test_file_assigns_fifo_and_seals_boxes(self):
        self._spawn_entry()
        self.ta.poll_spawns()
        recs = [_rec((T0 + 10) * 1000.0, reasoning="x" * 10,
                     text="step one", tools=[("Bash", {"command": "ls"})]),
                _rec((T0 + 20) * 1000.0, reasoning="y" * 10,
                     text="step two", tools=[("Read", {"path": "a.py"})])]
        self._agent_file("model-io-sess_subagent_agent_85205035-3294-4876-9862-5517f3511434.jsonl", recs)
        self.ta.assign_files()
        self.assertEqual(self.ta.agents[0].path.endswith("5517f3511434.jsonl"),
                         True)
        self.ta.poll_agents()
        a = self.ta.agents[0]
        self.assertEqual(a.req_n, 2)
        final = _rec((T0 + 30) * 1000.0, reason="stop",
                     text="All claims verified.")
        with open(a.path, "a") as f:
            f.write(json.dumps(final) + "\n")
        self.ta.poll_agents()
        self.assertTrue(a.done)
        # F64: the task thread saw ONLY the status line (the birth); the
        # boxes went to the cache — the thread stays clean
        self.assertEqual(len(self.tr.sent), 1)
        self.assertIn("/subagents", self.tr.sent[0][1])
        self.assertEqual(a.labels[0], "Agent 1.1")
        cache_p = agent_live.agent_cache_path(SID, T0, 1)
        with open(cache_p) as f:
            d = json.load(f)
        self.assertTrue(d["boxes"])
        # the status line closes with the result
        self.assertIn("done", self.tr.edits[-1][1])
        self.assertIn("All claims verified", self.tr.edits[-1][1])
        # the index (the /subagents source) is written with the state
        idx = agent_live.read_index(SID)
        self.assertEqual(idx["agents"][0]["state"], "done")

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

    def test_verbose_boxes_are_born_open_in_the_view(self):
        self.ta.cfg = dict(CFG, verbosity="verbose")
        self._spawn_entry()
        self.ta.poll_spawns()
        recs = [_rec((T0 + 10) * 1000.0, reason="stop", reasoning="deep",
                     text="done")]
        self._agent_file("model-io-sess_subagent_agent_33333333-3333-4333-8333-333333333ver.jsonl", recs)
        self.ta.assign_files()
        self.ta.poll_agents()
        # F64: /subagents materializes the boxes — born OPEN in verbose
        sent = []
        agent_live.subagent_show(dict(CFG, verbosity="verbose"), SID, T0, 1,
                                 send=lambda cfg, text, loud=None, markup=None:
                                 (sent.append((text, markup)), 8001)[1])
        self.assertTrue(sent)
        head, boxes = sent[0], sent[1:]
        self.assertIn("Agent 1", head[0])
        text, kb = boxes[0]
        self.assertIn("deep", text)  # born OPEN: content in the message
        self.assertEqual(kb["inline_keyboard"][-1][-1]["callback_data"],
                         "agr:%s:%d:1:1" % (SID, T0))
        # the phs were persisted: a second look does NOT resend
        sent2 = []
        agent_live.subagent_show(dict(CFG, verbosity="verbose"), SID, T0, 1,
                                 send=lambda cfg, text, loud=None, markup=None:
                                 (sent2.append((text, markup)), 8002)[1])
        self.assertEqual(len(sent2), 1)  # the status line only

    def test_cut_all_seals_the_tail_and_marks_the_cut(self):
        self._spawn_entry()
        self.ta.poll_spawns()
        self._agent_file("model-io-sess_subagent_agent_44444444-4444-4444-8444-444444444cut.jsonl",
                         [_rec((T0 + 10) * 1000.0, reasoning="half-done work")])
        self.ta.assign_files()
        self.ta.poll_agents()
        self.ta.cut_all("turn ended")
        self.assertIn("cut", self.tr.edits[-1][1])
        cache_p = agent_live.agent_cache_path(SID, T0, 1)
        with open(cache_p) as f:
            d = json.load(f)
        self.assertEqual(d["labels"], ["Agent 1.1"])
        self.assertTrue(d.get("perbox"))

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

    def test_pid_file_older_than_the_marker_is_not_alive(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(agent_live.watcher_pid_path(SID), "w") as f:
            f.write("4242")
        old = time.time() - 600
        os.utime(agent_live.watcher_pid_path(SID), (old, old))
        with open(config.marker_path(SID), "w") as f:
            json.dump({"token": "new-turn"}, f)   # marker NEWER than the pid
        self.assertFalse(agent_live.watcher_up(SID, proc_alive=lambda p: True))
        os.utime(agent_live.watcher_pid_path(SID), None)  # re-spawned now
        self.assertTrue(agent_live.watcher_up(SID, proc_alive=lambda p: True))

    def test_loop_survives_the_startup_grace_then_ends(self):
        support.patch(self, agent_live.config, "code_changed", lambda: False)
        clock = iter([1000.0, 1000.0, 1001.0, 1002.0,
                      1000.0 + agent_live.START_GRACE_S + 5, 5000.0, 5000.0,
                      5000.0, 5000.0, 5000.0, 5000.0])
        waits = []
        deps = {"now": lambda: next(clock), "running": lambda: {},
                "wait": lambda paths, timeout: waits.append(timeout),
                "send": lambda *a, **k: 1, "edit": lambda *a, **k: True,
                "delete": lambda *a, **k: None, "read_new": _read_file}
        agent_live.watch_agents(CFG, SID, "t", 1000.0, "/ws", deps=deps)
        # inside the grace the loop kept going (waits happened); past it,
        # with no running turn and no marker, it ended
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


if __name__ == "__main__":
    unittest.main()
