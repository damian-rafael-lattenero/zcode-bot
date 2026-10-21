# -*- coding: utf-8 -*-
"""F50 — fixes from the deep audit of 20/10 («read everything, hunt for
errors everywhere and fix them»).

Four agents + automated tools (pyflakes, import AST) read the whole
package. The bugs fixed here:

1. truncar_html (utils/formatting): send/_live_send/_live_edit truncated
   the ALREADY-built HTML at 3900 chars — the cut could land on an open
   <pre> (its closing tag lives at the end) and Telegram rejected the
   WHOLE message with 400 «Unclosed tag»: lost + the outbox retrying it
   forever. The inflation came from esc() (&→5x, <>→4x) over caps
   measured raw.
2. _partes_por_escape (reporting): _send_long_reply split at 3900 RAW
   and escaped afterwards — a part could inflate to ~5x and send
   truncated it: answer silently cut with the «(2/3)» lying.
3. BotSession.mode_set (core): prompt_worker wrote it after setMode
   but from_dict DISCARDED it — the skip_mode optimization was dead
   and every cold turn paid session/setMode again.
4. Atomic save_config: crash mid json.dump → truncated file →
   load_config fell to DEFAULTS (empty token) → deaf bot, no warning.
5. Answering «0» to a question: a negative index picked the
   LAST option out of the blue.
6. /cotb with n=0: phs[-1] pointed at the last reasoning.
7. read_quota_cache: max() over empty limits blew up /quota.
"""
import json
import os
import unittest
from unittest import mock

import support

from zcode_bot import config as zc_config
from zcode_bot.utils.formatting import esc, truncar_html
from zcode_bot import limites as lim


class TestTruncarHtml(unittest.TestCase):
    def test_short_intact(self):
        self.assertEqual(truncar_html("<b>hello</b>", 100), "<b>hello</b>")

    def test_open_pre_gets_closed(self):
        # the case of an HTML /diff or a CoT box with many escapes
        text = "<pre>" + "x" * 8000 + "</pre>"
        t = truncar_html(text, 3900)
        self.assertLessEqual(len(t), 3900 + len("</pre>"))
        self.assertTrue(t.startswith("<pre>"))
        self.assertTrue(t.endswith("</pre>"))  # Telegram accepts it whole

    def test_entity_split_by_the_cut(self):
        text = "a" * 3980 + " &amp; and more"
        t = truncar_html(text, 3900)
        self.assertFalse(t.endswith("&am"))    # broken entity, out
        self.assertNotIn("&am;", t)

    def test_tag_split_by_the_cut(self):
        text = "a" * 3899 + "<pre>rest"
        t = truncar_html(text, 3900)
        self.assertFalse(t.rstrip().endswith("<"))  # stray <pr, out

    def test_nesteds_closed_in_order(self):
        text = "<b>bold <pre>%s</pre></b>" % ("y" * 9000)
        t = truncar_html(text, 40)
        self.assertTrue(t.endswith("</pre></b>"))


class TestSendTruncatesSafely(unittest.TestCase):
    """send() uses truncar_html: whatever reaches Telegram always parses."""

    def setUp(self):
        self.zt = support.fresh(self)
        from zcode_bot.utils import http as zc_http
        self.params = []
        p = mock.patch.object(zc_http, "tg_api",
                              lambda cfg, method, params=None, timeout=35:
                              (self.params.append(params),
                               {"result": {"message_id": 1}})[1])
        p.start()
        self.addCleanup(p.stop)
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        enabled=True, loud_all=False)

    def test_the_text_that_travels_is_balanced_and_under_4096(self):
        from zcode_bot.utils import http as zc_http
        text = "<pre>%s</pre>" % ("&<>" * 5000)   # esc() inflates 5x
        zc_http.send(self.cfg, text)
        t = self.params[0]["text"]
        self.assertLessEqual(len(t), 4096)
        self.assertTrue(t.endswith("</pre>"))
        # no broken entities
        self.assertNotIn("&am;", t.replace("&amp;", ""))
        self.assertNotIn("&l", t.replace("&lt;", ""))


class TestPartsByEscape(unittest.TestCase):
    def test_inflated_text_parts_whole(self):
        from zcode_bot.reporting import _partes_por_escape
        cap = lim.CH_RESPUESTA - 300
        text = "&" * 20000           # esc() inflates it 5x
        parts = _partes_por_escape(text, cap)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(esc(p)), cap)
        self.assertEqual("".join(parts), text)   # nothing is lost

    def test_normal_text_one_part(self):
        from zcode_bot.reporting import _partes_por_escape
        cap = lim.CH_RESPUESTA - 300
        text = "short reply of the turn"
        self.assertEqual(_partes_por_escape(text, cap), [text])


class TestModeSetRoundTrip(unittest.TestCase):
    def test_survives_save_and_read(self):
        zt = support.fresh(self)
        from zcode_bot.core import (set_active_bot_session,
                                    active_bot_session)
        set_active_bot_session({"sid": "s1", "title": "t", "ws": "/w",
                                "mode_set": "yolo"})
        self.assertEqual(active_bot_session().get("mode_set"), "yolo")
        # without mode_set it doesn't appear (historical shape preserved)
        set_active_bot_session({"sid": "s2", "title": "t", "ws": "/w"})
        with open(os.path.join(zt.config.STATE_DIR,
                               zc_config.ARCH_SESION)) as f:
            self.assertNotIn("mode_set", json.load(f))


class TestSaveConfigAtomic(unittest.TestCase):
    def test_tmp_replaces_and_leaves_no_junk(self):
        zt = support.fresh(self)
        zt.save_config({"token": "abc", "enabled": True, "chat_id": 5})
        path = zt.config.CONFIG_PATH
        with open(path) as f:
            self.assertEqual(json.load(f)["token"], "abc")
        leftovers = [p for p in os.listdir(os.path.dirname(path))
                     if ".tmp." in p]
        self.assertEqual(leftovers, [])


class TestAnswerZeroAndQuestion(unittest.TestCase):
    def test_zero_does_not_pick_the_last_option(self):
        zt = support.fresh(self)
        from zcode_bot.commands import dispatch as zc_dispatch
        from zcode_bot.core import set_active_bot_session, _questions
        sid = support.T2
        set_active_bot_session({"sid": sid, "title": "CV",
                                "ws": support.PROJ_CV})
        _questions().ask(sid, "req-1", "¿Seguimos?",
                         [{"label": "Sí", "value": "si"},
                          {"label": "No", "value": "no"}])
        try:
            r = zc_dispatch.handle_free_text(
                dict(zt.DEFAULTS, token="T", chat_id=1), "0")
            # the "0" travels AS TEXT (the keyboard numbers from 1); with
            # the negative-index bug it answered "no" (the LAST option)
            self.assertIn("Answered: 0", r)
            self.assertNotIn("Respondido: no", r)
        finally:
            _questions().answer("req-1", None)   # clears the question


class TestCotbZero(unittest.TestCase):
    def test_n_zero_does_not_edit_the_last_box(self):
        zt = support.fresh(self)
        from zcode_bot import razonamiento as rz
        from zcode_bot.utils import http as zc_http
        sid, t0 = support.T1, 1760000000
        os.makedirs(os.path.dirname(rz.cache_path(sid, t0)), exist_ok=True)
        with open(rz.cache_path(sid, t0), "w") as f:
            json.dump({"sid": sid, "titulo": "t", "t0": t0,
                       "cajas": ["one", "two"], "rotulos": ["CoT 1.1",
                                                            "CoT 1.2"],
                       "ph": 555, "phs": [111, 222]}, f)
        edits = []
        p = mock.patch.object(zc_http, "_live_edit",
                              lambda cfg, ph, texto, markup=None:
                              (edits.append(ph), True)[1])
        p.start()
        self.addCleanup(p.stop)
        # n=0: out of range — shown as a notice over the GLOBAL ph
        r = zt.handle_command(
            dict(zt.DEFAULTS, token="T", chat_id=1),
            "/cotb %s %d 0" % (sid, t0))
        self.assertEqual(edits, [555])       # the global one, NOT 222
        self.assertNotEqual(edits, [222])


class TestHonestQueue(unittest.TestCase):
    """(H) An expired flock swallows nothing more: the owner finds out and
    /qgo doesn't duplicate by launching without dequeuing the item."""

    def setUp(self):
        self.zt = support.fresh(self)

    def _stuck_queue(self, mod):
        """A _store whose flock always expires — ONLY in the given module
        (the registry uses update_cross_process for other things)."""

        class _StoreStuck:
            def __init__(self, *a, **k):
                pass

            def update_cross_process(self, fn, default=None):
                raise OSError("flock deadline")

        p = mock.patch.object(mod, "_store", _StoreStuck)
        p.start()
        self.addCleanup(p.stop)

    def test_failed_put_notifies_and_does_not_lie(self):
        from zcode_bot.commands import handlers_tareas as ht
        from zcode_bot import workers as zw
        from zcode_bot.core import set_active_bot_session
        sid = support.T2
        set_active_bot_session({"sid": sid, "title": "CV",
                                "ws": support.PROJ_CV})
        self._stuck_queue(zw)   # _queue_put resolves _store in workers
        with mock.patch.object(ht, "running_turns", lambda: {sid}):
            r = ht.handle_free_text(
                dict(self.zt.DEFAULTS, token="T", chat_id=1), "hacé algo")
        self.assertIn("NOT queue your message", r)  # the honest notice

    def test_qgo_with_stuck_queue_launches_nothing(self):
        from zcode_bot.commands import handlers_tareas as ht
        from zcode_bot import workers as zw
        sid = support.T2
        zw._queue_put(sid, {"sid": sid, "title": "CV",
                            "ws": support.PROJ_CV}, "prompt encolado")
        spawns = []
        with mock.patch.object(ht, "_spawn_worker",
                               lambda cfg, act, text:
                               (spawns.append(text), 777)[1]):
            self._stuck_queue(ht)
            r = ht._cmd_qgo(dict(self.zt.DEFAULTS, token="T", chat_id=1),
                            "%s:0" % sid)
        self.assertIn("stuck", r)
        self.assertEqual(spawns, [])            # nothing launched: no duplicate


class TestQuotaLimitsEmpty(unittest.TestCase):
    def test_empty_limits_do_not_blow_up(self):
        zt = support.fresh(self)
        from zcode_bot import reporting
        # fake leveldb in the fixture (the real path is absolute:
        # a conditional expanduser redirects it here)
        root = zt.config.BASE
        ldir = os.path.join(root, "leveldb")
        os.makedirs(ldir, exist_ok=True)
        with open(os.path.join(ldir, "000003.log"), "wb") as f:
            f.write(b'garbage "quota":{"level":"X","limits":[]} trash')
        with open(os.path.join(ldir, "000004.log"), "wb") as f:
            f.write(b'"quota":{"level":"X","limits":'
                    b'[{"type":"CREDIT_LIMIT","nextResetTime":99,'
                    b'"number":5,"currentValue":1,"usage":10,'
                    b'"remaining":9,"percentage":10}]}')
        real = os.path.expanduser

        def fake_exp(p):
            if p.startswith("~/Library/Application Support/ZCode"):
                return ldir   # the fixture's fake leveldb
            return real(p)
        with mock.patch("os.path.expanduser", fake_exp):
            r = reporting.read_quota_cache()
        self.assertIsNotNone(r)   # the healthy one won, no ValueError
        self.assertEqual(r["windows"][0]["hours"], 5)


class TestRegisterRace(unittest.TestCase):
    """(agent workers/live_view #1) register()'s return value is the
    anti-duplication signal: losing the race to another spawn must
    ENQUEUE, not run a second turn on the same session."""

    def test_losing_the_race_enqueues_and_does_not_start(self):
        zt = support.fresh(self)
        from zcode_bot import workers as zw

        class _BlindRegistry:
            """get() doesn't see the other worker the FIRST time (the race) —
            register() returns False because it's ALREADY registered."""
            def __init__(self, real):
                self.real = real
                self.times = 0

            def get(self, sid):
                self.times += 1
                return None if self.times == 1 else self.real.get(sid)

            def __getattr__(self, n):
                return getattr(self.real, n)

        reg = zt._workers()
        # a worker from ANOTHER pid already registered the session
        reg.register(support.T2, {"t0": 1, "pid": 999999,
                                  "titulo": "CV", "prompt": "x"})
        enqueued = []
        with mock.patch.object(zw, "_workers",
                               lambda: _BlindRegistry(reg)), \
             mock.patch.object(zw, "_queue_put",
                               lambda s, a, t: enqueued.append(t) or True), \
             mock.patch.object(zw, "zc_proto",
                               type("M", (), {"make": staticmethod(
                                   lambda **k: (_ for _ in ()).throw(
                                       AssertionError("should not have "
                                                      "reached the runtime")))})):
            zw.prompt_worker(dict(zt.DEFAULTS, token="T", chat_id=1),
                             {"sid": support.T2, "title": "CV",
                              "ws": support.PROJ_CV}, "race message")
        self.assertEqual(enqueued, ["race message"])


class TestFifoOfFailedSpawn(unittest.TestCase):
    """(agent #2) The item whose spawn failed goes back to the FRONT —
    putting it at the end reversed the order of the user's messages."""

    def test_failed_spawn_leaves_the_queue_in_order(self):
        zt = support.fresh(self)
        from zcode_bot import workers as zw
        sid = support.T2
        for txt in ("A", "B", "C"):
            zw._queue_put(sid, {"sid": sid, "title": "CV",
                                "ws": support.PROJ_CV}, txt)
        cfg = dict(zt.DEFAULTS, token="T", chat_id=1)
        with mock.patch.object(zw, "_spawn_worker", lambda *a: None):
            r = zw._queue_launch_next(cfg, sid)
        self.assertIsNone(r)
        self.assertEqual([i["text"] for i in zw._queue_take(sid)],
                         ["A", "B", "C"])   # A still first


class TestCombinedStop(unittest.TestCase):
    """(agent razonamiento/reporting #1, CRITICAL) /stop with a running
    turn AND a sleeping queue at once blew up with TypeError — the
    emergency command must not fail right there."""

    def test_running_and_queued_together_build_one_listing(self):
        zt = support.fresh(self)
        from zcode_bot import reporting
        from zcode_bot import workers as zw
        from zcode_bot.core import _workers
        # one worker running (items)…
        _workers().register(support.T1,
                            {"t0": 1, "pid": 424242, "titulo": "testing",
                             "prompt": "x"})
        # …and a sleeping queue from ANOTHER session (extras)
        zw._queue_put(support.T2, {"sid": support.T2, "title": "CV",
                                   "ws": support.PROJ_CV}, "pending")
        r = reporting.stop_list_report()
        text, kb = r if isinstance(r, tuple) else (r, None)
        self.assertIn("Phone runs", text)
        self.assertIn("Queued messages", text)        # both, no blow-up
        buttons = [b["text"] for f in (kb or {}).get("inline_keyboard", [])
                   for b in f]
        self.assertTrue(any("clear queue" in b for b in buttons))


class TestBoxesWithEscapables(unittest.TestCase):
    """(inflation family) boxes and viewer with code inside (& < >) —
    they arrive WHOLE, not cut by send's truncation."""

    def setUp(self):
        self.zt = support.fresh(self)

    def test_show_parts_by_escape(self):
        from zcode_bot import razonamiento as rz
        sid, t0 = support.T1, 1760000000
        box = "&" * 3000                 # esc() inflates it to 15000
        rz.save_cache(sid, t0, "testing", [box], ["CoT 1.1"], ph=555,
                      phs=[555])
        r = rz.show(sid, t0, 1)
        if isinstance(r, tuple):
            r = r[0]
        for part in r.split("<pre>")[1:]:
            # each visible part fits WHOLE in one message
            self.assertLessEqual(len(part.split("</pre>")[0]),
                                 lim.CH_RESPUESTA)
        self.assertTrue(r.count("&amp;") >= 100)  # real content, not short

    def test_tool_box_truncates_by_escape(self):
        from zcode_bot.messages import _tool_box
        box = _tool_box("Bash", {"command": "&" * 9000,
                                  "output": "ok"})
        self.assertIn("&amp;", box)
        self.assertLessEqual(len(box), lim.CH_CAJA + 800 + 600)

    def test_html_clip_max_prefix(self):
        from zcode_bot.utils.formatting import recorte_html
        self.assertEqual(recorte_html("hello", 100), "hello")
        t = recorte_html("&" * 1000, 500)
        self.assertLessEqual(len(esc(t)), 500)     # the escape FITS
        self.assertEqual(t, "&" * len(t))          # clean prefix


class TestCacheAtomic(unittest.TestCase):
    def test_save_leaves_no_truncated_file_in_sight(self):
        zt = support.fresh(self)
        from zcode_bot import razonamiento as rz
        sid, t0 = support.T1, 1760000000
        rz.save_cache(sid, t0, "t", ["box"], ["CoT 1.1"], vivo=True)
        # a reader mid-write NEVER sees truncation: the final path
        # always holds complete JSON (tmp + replace)
        with open(rz.cache_path(sid, t0)) as f:
            d = json.load(f)
        self.assertTrue(d["vivo"])
        self.assertEqual(d["cajas"], ["box"])
        rz.close_cache(sid, t0)
        with open(rz.cache_path(sid, t0)) as f:
            self.assertTrue(json.load(f)["finalizado"])
        leftovers = [p for p in os.listdir(zt.config.STATE_DIR)
                     if ".tmp." in p]
        self.assertEqual(leftovers, [])


class TestLineStamp(unittest.TestCase):
    """F50b — Damian: «every file you show, diff or plain, with its line
    count and green and red circles — all of them»."""

    def test_stamp_with_diff_and_common(self):
        from zcode_bot.utils.formatting import sello_lineas
        self.assertEqual(sello_lineas(3, 1, 40), "🟢 +3 · 🔴 −1 · 40 lines")
        self.assertEqual(sello_lineas(3, 0, 40), "🟢 +3 · 40 lines")
        self.assertEqual(sello_lineas(0, 0, 40), "40 lines")     # plain
        self.assertEqual(sello_lineas("-", "-"), "—")           # binary

    def test_write_box_carries_the_stamp(self):
        from zcode_bot.messages import _tool_box
        box = _tool_box("Write", {"content": "one\ntwo\nthree"})
        self.assertIn("🟢 +3 · 3 lines", box)
        self.assertTrue(box.count("🟢") >= 3)   # and the green lines

    def test_edit_box_carries_the_diffstat(self):
        from zcode_bot.messages import _tool_box
        box = _tool_box("Edit", {"old_string": "a\nb",
                                  "new_string": "a\nc\nd"})
        self.assertIn("🟢 +2", box)
        self.assertIn("🔴 −1", box)


class TestPollerBatchSurvives(unittest.IsolatedAsyncioTestCase):
    """(G) An exception in message k confirmed the offset of the WHOLE
    batch: the queue (k+1..n) was lost with no reprocessing. Now the
    poison message is logged and the rest of the batch runs."""

    async def test_the_poisonous_one_does_not_drag_the_batch_queue(self):
        from test_poller import FakeDeps, _msg

        class _Deps(FakeDeps):
            def process(self, cfg, msg):
                if msg.get("text") == "/b":    # only /b is poisonous
                    raise RuntimeError("boom")
                super().process(cfg, msg)

        d = _Deps(batches=[[_msg(1, "/a"), _msg(2, "/b"), _msg(3, "/c")]])
        await d.run()
        self.assertEqual(d.processed, ["/a", "/c"])  # b blew up, c ran
        self.assertEqual(d.saved, [101])              # and the batch advanced
        self.assertTrue(any("process crashed" in e for e in d.errors))


if __name__ == "__main__":
    unittest.main()
