"""F33 — espectador: the Mac's turn streams to the phone via the rollout.

Coverage: record parser (tool boxes yes, intermediate narration yes,
final answer NO — F32 sends that), F20 correlation (old turn/compact
ignored), incremental reading with offset (partial line not consumed,
monster skipped), end by consumed marker, hard cap, mac_espectador
knob and spawn from on_user_prompt (test_f15). No network: recorded
sends, fake wait, sequence clock.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import espectador as esp  # noqa: E402

# The rollout carries startedAt as an ISO STRING with Z — the original
# F33 ran the tests with numbers and the real format never made it
# past the correlation (mute espectador, incident 19/10). The tests
# use THE REAL FORMAT, not an agreed-upon one.
_T0 = datetime(2026, 10, 19, 15, 0, 0, tzinfo=timezone.utc)
_T0_MS = _T0.timestamp() * 1000.0


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _record(turn_id="t1", started_at=None, finish=None, text=None,
            tools=None, source="main_turn"):
    return {"querySource": source, "turnId": turn_id,
            "startedAt": started_at or _iso(_T0),
            "response": {"finishReason": finish, "text": text or "",
                         "toolCalls": tools or []}}


def _bash(command="ls -la"):
    return {"id": "c1", "name": "Bash", "input": {"command": command}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = dict(self.zt.DEFAULTS)
        self.cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
        self.sends = []
        self.st = {"turn_id": None, "t0_ms": _T0_MS, "step": 0,
                   "title": "proj", "cfg": self.cfg, "noticed": False}
        os.makedirs(os.path.join(self.zt.config.BASE, "cli", "rollout"),
                    exist_ok=True)
        os.makedirs(self.zt.config.STATE_DIR, exist_ok=True)
        self.rollout = self.zt.config.rollout_path("s1")

    def _send(self):
        return lambda text: self.sends.append(text) or 1

    def _write(self, *records):
        with open(self.rollout, "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")


class TestProcess(_Base):
    """The parser of one rollout record → chat messages."""

    def test_tool_call_sends_box(self):
        n = esp._process(_record(tools=[_bash("ls -la /tmp")]), self.st,
                         self._send())
        self.assertEqual(n, 1)
        self.assertIn("(spectator)", self.sends[0])  # label only the 1st time
        self.assertIn("🔧 <b>Bash</b>", self.sends[1])
        self.assertIn("ls -la /tmp", self.sends[1])
        self.assertIn("· <b>1</b>", self.sends[1])
        self.assertEqual(self.st["turn_id"], "t1")

    def test_intermediate_text_yes_final_no(self):
        esp._process(_record(finish="tool-calls", text="voy a mirar el diff"),
                     self.st, self._send())
        self.assertIn("voy a mirar el diff", self.sends[1])
        before = len(self.sends)
        esp._process(_record(finish="stop", text="respuesta final completa"),
                     self.st, self._send())
        # the FINAL one is not sent by the espectador: on_stop/F32 delivers it
        self.assertEqual(len(self.sends), before)
        self.assertNotIn("respuesta final completa", "".join(self.sends))

    def test_old_turn_ignored(self):
        n = esp._process(_record(turn_id="viejo",
                                 started_at=_iso(datetime(
                                     2026, 10, 19, 14, 0, 0,
                                     tzinfo=timezone.utc)),
                                 tools=[_bash()]), self.st, self._send())
        self.assertEqual((n, self.sends, self.st["turn_id"]), (0, [], None))

    def test_started_at_iso_string_correlates(self):
        """THE bug of the original F33: startedAt arrives ISO with Z (the
        rollout's real format) — with isinstance((int, float)) the
        correlation died and the espectador sent NOTHING (incident
        19/10)."""
        n = esp._process(_record(tools=[_bash()]), self.st, self._send())
        self.assertEqual(n, 1)
        self.assertEqual(self.st["turn_id"], "t1")
        self.assertIn("🔧 <b>Bash</b>", self.sends[1])

    def test_started_at_broken_or_absent_does_not_correlate(self):
        self.assertEqual(esp._process(
            _record(started_at="no-es-una-fecha", tools=[_bash()]),
            self.st, self._send()), 0)
        self.assertEqual(self.st["turn_id"], None)
        self.assertEqual(esp._ts_ms(None), None)
        self.assertEqual(esp._ts_ms(""), None)

    def test_started_at_numeric_epoch_also_works(self):
        n = esp._process(_record(started_at=_T0_MS, tools=[_bash()]),
                         self.st, self._send())
        self.assertEqual(n, 1)
        self.assertEqual(self.st["turn_id"], "t1")

    def test_correlation_pins_and_filters_others(self):
        esp._process(_record(turn_id="t1"), self.st, self._send())
        self.assertEqual(self.st["turn_id"], "t1")
        before = len(self.sends)
        esp._process(_record(turn_id="t2", tools=[_bash()]), self.st,
                     self._send())
        self.assertEqual(len(self.sends), before)  # crossed turn: out

    def test_compact_ignored(self):
        n = esp._process(_record(source="compact", tools=[_bash()]),
                         self.st, self._send())
        self.assertEqual((n, self.sends), (0, []))

    def test_tools_stream_off(self):
        self.cfg["tools_stream"] = False
        esp._process(_record(finish="tool-calls", text="analizo",
                             tools=[_bash()]), self.st, self._send())
        self.assertEqual(len(self.sends), 2)  # label + narration, no box
        self.assertNotIn("🔧", "".join(self.sends))

    def test_minimal_box_preset_filters(self):
        self.cfg["tools_caja_filtro"] = "minimo"
        esp._process(_record(tools=[{"id": "c", "name": "Grep",
                                     "input": {"pattern": "x"}}]),
                     self.st, self._send())
        self.assertEqual(self.sends, [])  # Grep does nothing: no box


class TestReadNew(_Base):
    """The incremental tail: offset, partial line, monster record."""

    def test_partial_line_not_consumed(self):
        self._write(_record(turn_id="a"))
        with open(self.rollout, "a") as f:
            f.write(json.dumps(_record(turn_id="b"))[:20])  # half-written
        records, pos = esp._read_new(self.rollout, 0)
        self.assertEqual([r["turnId"] for r in records], ["a"])
        records2, pos2 = esp._read_new(self.rollout, pos)
        self.assertEqual(records2, [])
        with open(self.rollout, "a") as f:
            f.write(json.dumps(_record(turn_id="b"))[20:] + "\n")
        records3, _ = esp._read_new(self.rollout, pos)
        self.assertEqual([r["turnId"] for r in records3], ["b"])

    def test_monster_record_skipped_and_offset_advances(self):
        line = "x" * 5000 + "\n"
        with open(self.rollout, "w") as f:
            f.write(json.dumps(_record(turn_id="ok")) + "\n" + line)
        with mock.patch.object(esp, "REG_CAP", 200):
            records, pos = esp._read_new(self.rollout, 0)
        self.assertEqual([r["turnId"] for r in records], ["ok"])
        self.assertEqual(pos, os.path.getsize(self.rollout))

    def test_no_news_and_missing_file(self):
        self.assertEqual(esp._read_new(self.rollout, 0), ([], 0))
        self._write(_record())
        records, pos = esp._read_new(self.rollout, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(esp._read_new(self.rollout, pos), ([], pos))


class TestWatch(_Base):
    """The loop: end by marker, hard cap, label only once."""

    def _deps(self, marker=True, waits=None, times=None):
        d = {"send": lambda cfg, t, loud=None: self.sends.append(t) or True,
             "sleep": lambda s: None,
             "now": self._clock(times or [0.0] * 12),
             "marker_alive": lambda: marker if isinstance(marker, bool)
             else marker(),
             "wait": waits if waits is not None
             else (lambda paths, timeout: False)}
        return d

    def _clock(self, sequence):
        state = {"i": 0}

        def f():
            t = sequence[min(state["i"], len(sequence) - 1)]
            state["i"] += 1
            return t
        return f

    def test_end_by_consumed_marker(self):
        steps = esp.watch("s1", "/fake/proj", self.cfg,
                          self._deps(marker=False))
        self.assertEqual(steps, 0)
        self.assertEqual(self.sends, [])  # no turn: not even the label

    def test_hard_cap_cuts(self):
        steps = esp.watch("s1", "/fake/proj", self.cfg, self._deps(
            times=[0.0, 0.0, esp.CAP_S + 1.0, esp.CAP_S + 1.0,
                   esp.CAP_S + 1.0]))
        self.assertEqual(steps, 0)

    def test_marker_flow_and_label_once(self):
        state = {"rounds": 0}

        def marker():
            state["rounds"] += 1
            return state["rounds"] < 3

        def wait(paths, timeout):
            if state["rounds"] == 1:
                self._write(_record(tools=[_bash("git status")]))
            return False

        steps = esp.watch("s1", "/fake/proj", self.cfg,
                          self._deps(marker=marker, waits=wait))
        self.assertEqual(steps, 1)
        self.assertEqual(self.sends[0].count("spectator"), 1)
        self.assertEqual(len([s for s in self.sends if "🔧" in s]), 1)


class TestKnobAndSpawn(_Base):
    """mac_espectador turns the whole feature off (config knob)."""

    def test_knob_default_on(self):
        self.assertTrue(self.zt.DEFAULTS.get("mac_espectador"))

    def test_marker_and_rollout_paths(self):
        self.assertEqual(esp.marker_path("s1"),
                         self.zt.marker_path("s1"))  # same formula (hooks)


if __name__ == "__main__":
    unittest.main()
