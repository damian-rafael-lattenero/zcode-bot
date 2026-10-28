"""F85 — the chat hears the wire.

(a) The closing bundle dies: cots land in the moment or wait in the
cache — «los cot llegan en el momento no se acumulen, salvo que sea  # noqa: spanish — Damian's rule, quoted
con cambio de sesion con el command task» (28/10). The collapsed  # noqa: spanish — Damian's rule, quoted (continuation)
landing survives ONLY on the task-switch re-attach (the catchup path).
(b) The z.ai connection speaks: the fork's pills (retry curve 2^n,
f85a67f6) live on the Mac while the chat was deaf to his brief
internet cut. zconn feeds the CLI log's DOWN/UP pairs into one episode
machine: one ⚠️ with the reason, one ✅ with how long it lasted."""  # noqa: spanish — Damian's rule, quoted
import json
import os
import unittest

import support  # src on sys.path (deterministic)

from zcode_bot import zconn


def _ev(ts, ev, reason=""):
    return (ts, "session_x", ev, {"context":
                                   {"statusMessage": reason}} if reason
            else {})


class TestTheEpisodeMachine(unittest.TestCase):
    def test_one_down_per_episode_then_one_up(self):
        t = zconn.ConnTrack()
        self.assertEqual(t.feed(10.0, "model.network.failed", "ETIMEDOUT"),
                         [("down", "ETIMEDOUT", 10.0)])
        # the warn PAIR (request.failed) is not a second episode
        self.assertEqual(t.feed(10.1, "model.request.failed", "x"), [])
        # retries keep quiet; the recovery SPEAKS (42s cut), the second
        # completed is nothing new
        self.assertEqual(t.feed(14.0, "model.network.failed", "502"), [])
        self.assertEqual(t.feed(52.0, "model.network.completed"),
                         [("up", 10.0, 52.0)])
        self.assertEqual(t.feed(53.0, "model.network.completed"), [])
        # ... and it speaks again only on the NEXT fresh episode
        self.assertEqual(t.feed(600.0, "model.network.failed", "down"),
                         [("down", "down", 600.0)])

    def test_up_reports_the_duration(self):
        t = zconn.ConnTrack()
        t.feed(100.0, "model.network.failed", "no route")
        self.assertEqual(t.feed(142.0, "model.network.completed"),
                         [("up", 100.0, 142.0)])

    def test_sputters_are_one_episode(self):
        t = zconn.ConnTrack()
        t.feed(100.0, "model.network.failed", "first")
        t.feed(130.0, "model.network.completed")          # up after 30s
        # the SAME sick wire fails again 10s later: quiet (one story)
        self.assertEqual(t.feed(140.0, "model.network.failed", "again"), [])
        # the final recovery reports the WHOLE episode (100 → 200)
        self.assertEqual(t.feed(200.0, "model.network.completed"),
                         [("up", 100.0, 200.0)])

    def test_fmt_dur(self):
        self.assertEqual(zconn._fmt_dur(42), "42s")
        self.assertEqual(zconn._fmt_dur(200), "3m 20s")


class TestTheCycleHook(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False)
        self.sent = []
        import unittest.mock as mock
        self._m = mock.patch.object(
            zconn.uhttp, "send",
            lambda cfg, text, loud=None, markup=None, a_outbox=True:
            (self.sent.append((text, loud)), 900)[1])
        self._m.start()
        self.addCleanup(self._m.stop)

    def _st_path(self):
        import zcode_bot.config as c
        return os.path.join(c.STATE_DIR, zconn.CHECK_STATE)

    def test_first_run_arms_silently_no_past_replay(self):
        # a log full of yesterday's failures must NOT replay
        def iter_ev(base, **kw):
            yield _ev(1.0, "model.network.failed", "old")
            yield _ev(2.0, "model.network.completed")
        self.assertEqual(zconn.listen(self.cfg, iter_ev=iter_ev, now=100.0),
                         [])
        d = json.load(open(self._st_path()))
        self.assertAlmostEqual(d["since"], 100.0)

    def test_transitions_speak_and_state_advances(self):
        zconn.listen(self.cfg, iter_ev=lambda b, **k: iter(()), now=100.0)
        events = [_ev(101.0, "model.network.failed",
                      "Cannot connect to API: read ETIMEDOUT"),
                  _ev(101.2, "model.request.failed",
                      "Cannot connect to API: read ETIMEDOUT"),
                  _ev(130.0, "model.network.completed")]
        calls = [0]

        def iter_ev(base, needles=(), since_epoch=None, **kw):
            # only the second pass sees the recovery
            out = events if calls[0] else events[:2]
            calls[0] += 1
            for e in out:
                if since_epoch is None or e[0] >= since_epoch - 1:
                    yield e
        got = zconn.listen(self.cfg, iter_ev=iter_ev, now=102.0)
        self.assertEqual([n[0] for n in got], ["down"])
        got2 = zconn.listen(self.cfg, iter_ev=iter_ev, now=131.0)
        self.assertEqual([n[0] for n in got2], ["up"])
        # the messages: reason in the down, duration in the up
        self.assertIn("ETIMEDOUT", self.sent[0][0])
        self.assertTrue(self.sent[0][1])          # loud — he asked to know
        self.assertIn("29s", self.sent[1][0])
        self.assertTrue(self.sent[1][1])

    def test_a_dead_mac_parks_both_and_they_land_together(self):
        """The sends go through uhttp.send: during a total outage they
        park in the outbox (F83) and land when the wire returns — the
        story still gets told (the mock here only proves the calls)."""
        zconn.listen(self.cfg, iter_ev=lambda b, **k: iter(()), now=1.0)

        def iter_ev(base, **kw):
            yield _ev(2.0, "model.network.failed", "ENOTFOUND")
            yield _ev(47.0, "model.network.completed")
        got = zconn.listen(self.cfg, iter_ev=iter_ev, now=48.0)
        self.assertEqual([n[0] for n in got], ["down", "up"])
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()


class TestBashOutputs(unittest.TestCase):
    """F86 — the command outputs reach the chat (the pytest tail he
    never saw). Pure pieces: the extractor and the message text."""

    def _bash(self, status, command="pytest -q", output=None):
        st = {"status": status,
              "input": {"command": command}}
        if output is not None:
            st["output"] = output
        return {"type": "tool", "tool": "Bash", "state": st}

    def test_settled_bash_with_dict_output(self):
        from zcode_bot import reasoning as R
        m = {"id": "m9", "info": {"role": "assistant"},
             "parts": [self._bash("completed",
                                  output={"stdout": "7 passed\n",
                                          "stderr": "", "exitCode": 0})]}
        got = R._bash_outputs([m], set())
        self.assertEqual(len(got), 1)
        key, cmd, rc, status, tail = got[0]
        self.assertEqual(key, "m9:0")
        self.assertEqual(cmd, "pytest -q")
        self.assertEqual(rc, 0)
        self.assertEqual(status, "completed")
        self.assertIn("7 passed", tail)

    def test_running_and_non_bash_are_invisible(self):
        from zcode_bot import reasoning as R
        msgs = [{"id": "m1", "parts": [
            self._bash("running"),
            {"type": "tool", "tool": "Read",
             "state": {"status": "completed"}}]}]
        self.assertEqual(R._bash_outputs(msgs, set()), [])

    def test_seen_keys_never_return_and_the_seed_covers_the_past(self):
        from zcode_bot import reasoning as R
        m = {"id": "m1", "parts": [self._bash("completed",
                                              output={"stdout": "x",
                                                      "exitCode": 1})]}
        seed = R._bash_keys([m])
        self.assertEqual(R._bash_outputs([m], seed), [])   # respawn: quiet
        fresh = {"id": "m2", "parts": [self._bash("completed",
                                                  output={"stdout": "y",
                                                          "exitCode": 0})]}
        self.assertEqual([g[0] for g in R._bash_outputs([m, fresh], seed)],
                         ["m2:0"])

    def test_the_message_text(self):
        from zcode_bot import reasoning as R
        t = R._bash_out_text("pytest tests/ -q", 0, "completed",
                             "7 passed in 0.2s")
        self.assertIn("<code>pytest tests/ -q</code> — RC 0", t)
        self.assertIn("<pre>7 passed in 0.2s</pre>", t)
        # the store's form: a STRING output, no exitCode — the status
        # is the verdict (✓ / ⚠️), the tail is capped at 400
        t2 = R._bash_out_text("big", None, "completed", "z" * 900)
        self.assertIn(" — ✓", t2)
        self.assertLess(len(t2), 700)
        t3 = R._bash_out_text("boom", None, "failed", "Err")
        self.assertIn("⚠️ failed", t3)


class TestBashOutputsInTheLoop(unittest.TestCase):
    """F86 at the watcher: a Bash that settles mid-turn sends ONE
    output message; everything settled before the spawn stays quiet."""

    def setUp(self):
        import zcode_bot.reasoning as R
        self.R = R
        self.zt = support.fresh(self)
        self.sends = []
        support.patch(self, R.uhttp, "send",
                      lambda cfg, text, loud=None, markup=None,
                      a_outbox=True:
                      (self.sends.append(text), 777)[1])
        support.patch(self, R.uhttp, "_live_edit",
                      lambda cfg, ph, text, markup=None: True)
        support.patch(self, R, "_live_line_text", lambda *a: "")
        self.naps = []
        support.patch(self, R.time, "sleep",
                      lambda s: self.naps.append(s))
        self.SID = "session_F86LOOP"
        self.T0 = 1793000000.0
        import zcode_bot.core as zc_core
        zc_core.set_active_bot_session({"sid": self.SID, "title": "f86",
                                        "ws": "/w"})
        with open(R.marker_path(self.SID), "w") as f:
            f.write("{}")
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False, cot_stream=True)

    def _bash_part(self, status, output=None):
        st = {"status": status, "input": {"command": "pytest -q"}}
        if output is not None:
            st["output"] = output
        return {"type": "tool", "tool": "Bash", "state": st}

    def test_settling_mid_turn_sends_one_output_message(self):
        R = self.R
        done = {"stdout": "974 passed in 12s", "stderr": "",
                "exitCode": 0}

        class _P:
            def __init__(self, marker):
                self.marker, self.n = marker, 0

            def request(self, method, params=None, timeout=None):
                if method == "session/resume":
                    return {"result": {}}
                if method == "session/messages":
                    self.n += 1
                    if self.n == 1:      # running: nothing to say yet
                        part = self._run()
                    elif self.n == 2:    # settled: the tail speaks
                        part = {"type": "tool", "tool": "Bash",
                                "state": {"status": "completed",
                                          "input": {"command":
                                                    "pytest -q"},
                                          "output": done}}
                    else:
                        import os
                        try:
                            os.remove(self.marker)
                        except OSError:
                            pass
                        return {"result": {"messages": [
                            {"id": "u", "info": {"role": "user"},
                             "parts": [{"type": "text", "text": "p"}]}]}}
                    return {"result": {"messages": [
                        {"id": "u", "info": {"role": "user"},
                         "parts": [{"type": "text", "text": "p"}]},
                        {"id": "a", "info": {"role": "assistant"},
                         "parts": [part]}]}}

            def _run(self):
                return {"type": "tool", "tool": "Bash",
                        "state": {"status": "running",
                                  "input": {"command": "pytest -q"}}}

            def close(self):
                pass

        support.patch(self, R.zc_proto, "make",
                      lambda cwd=None, **kw: _P(R.marker_path(self.SID)))
        R.run_watcher(self.cfg, self.SID, "f86", self.T0, "/w")
        outs = [t for t in self.sends if "974 passed" in t]
        self.assertEqual(len(outs), 1)
        self.assertIn("pytest -q", outs[0])
        self.assertIn(" — RC 0", outs[0])
