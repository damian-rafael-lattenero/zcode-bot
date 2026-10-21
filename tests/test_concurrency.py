"""Concurrency tests: AtomicStore, WorkersRegistry, QuestionFiles
and the ProtoClient deadlock REGRESSION (2026-10-12) with a fake server.

Async suite (ADR-009): zero sleeps — coordination is reactive
(asynkit.call/wait_until/Chan); producers and consumers scale with
gather, not with hand-written threads.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import asynkit
import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

from zcode_bot import state as zc_state          # noqa: E402
from zcode_bot import concurrency as zc_conc     # noqa: E402
import zcode_proto as zp                          # noqa: E402


class TestAtomicStore(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zcstate-")
        self.path = os.path.join(self.dir, "estado.json")

    async def test_write_read_roundtrip(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"a": 1, "b": ["x", "y"]})
        self.assertEqual(st.read(), {"a": 1, "b": ["x", "y"]})

    async def test_no_tmp_left_nor_truncated_state(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"n": 1})
        # simulated crash: an exception mid-update does not corrupt
        def boom(cur):
            raise RuntimeError("crash mid-write")
        with self.assertRaises(RuntimeError):
            st.update(boom, {})
        self.assertEqual(st.read(), {"n": 1})  # intact
        self.assertEqual([f for f in os.listdir(self.dir) if ".tmp" in f], [])

    async def test_update_rmw(self):
        st = zc_state.AtomicStore(self.path)
        st.write({"count": 0})
        for _ in range(3):
            st.update(lambda d: {"count": d["count"] + 1}, {})
        self.assertEqual(st.read()["count"], 3)

    async def test_update_cross_process(self):
        # 4 "processes" (executor) × 10 concurrent puts: none is lost
        st = zc_state.AtomicStore(self.path)
        st.write({"n": 0})

        async def worker():
            def put10():
                for _ in range(10):
                    st.update_cross_process(lambda d: {"n": d["n"] + 1},
                                            {"n": 0})
            await asynkit.call(put10)
        await asyncio.gather(*[worker() for _ in range(4)])
        self.assertEqual(st.read()["n"], 40)


class TestWorkersRegistry(unittest.IsolatedAsyncioTestCase):
    """F16: file-backed (bot-workers.json with flock) — same API as the
    thread era, but survives poller restarts and prunes dead entries."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="zcworkers-")
        self.path = os.path.join(self.dir, "bot-workers.json")

    def _r(self):
        return zc_conc.WorkersRegistry(self.path)

    async def test_register_dedupe_and_rekey(self):
        r = self._r()
        self.assertTrue(r.register("fresh", {"t0": 1}))
        self.assertFalse(r.register("fresh", {"t0": 2}))  # dedupe → enqueue
        r.rekey("fresh", "sess_real_1")                  # fix orphan bug
        self.assertIsNone(r.get("fresh"))
        # the FILE has the entry with pid (F16 contract)
        ent = json.load(open(self.path))["sess_real_1"]
        self.assertEqual(ent["pid"], os.getpid())

    async def test_two_registries_see_the_same(self):
        # the real case: poller and worker-process, distinct instances
        a, b = self._r(), self._r()
        self.assertTrue(a.register("s1", {"title": "testing"}))
        self.assertIsNotNone(b.get("s1"))  # b sees a's registration
        self.assertFalse(b.register("s1", {"title": "dup"}))  # cross dedup

    async def test_stop_flag(self):
        r = self._r()
        r.register("s1", {"title": "testing"})
        got = r.flag_stop("s1")
        self.assertEqual(got, "testing")  # F16: returns the title
        self.assertTrue(r.stopped("s1"))
        self.assertIsNone(r.flag_stop("s2"))  # does not exist
        r.unflag_stop("s1")
        self.assertFalse(r.stopped("s1"))
        r.pop("s1")
        self.assertTrue(r.empty())

    async def test_prune_of_dead_worker(self):
        # worker crash without finally: the entry stays with a dead PID →
        # any reader prunes it (otherwise: dedup broken forever)
        r = self._r()
        dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_proc.wait()
        st = zc_state.AtomicStore(self.path)
        st.write({"s_dead": {"pid": dead_proc.pid, "t0": time.time(),
                               "title": "x"}})
        self.assertIsNone(r.get("s_dead"))
        self.assertTrue(r.empty())

    async def test_prune_by_age_anti_pid_reuse(self):
        # LIVE PID (foreign process) but an entry older than 4h: it is
        # pruned (the pid may be a recycled other process). The reader's
        # own pid is exempt — its workers are always of known reliability.
        r = self._r()
        alive = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            st = zc_state.AtomicStore(self.path)
            st.write({"s_old": {"pid": alive.pid,
                                  "t0": time.time() - r.MAX_AGE_S - 60,
                                  "title": "x"}})
            self.assertIsNone(r.get("s_old"))
        finally:
            alive.kill()
            alive.wait()

    async def test_items_is_sorted_snapshot(self):
        r = self._r()
        r.register("a", {"t0": 5})
        items = r.items()
        r.register("b", {"t0": 1})
        self.assertEqual(len(items), 1)  # a copy, not a live view
        self.assertEqual([k for k, _ in r.items()], ["b", "a"])  # by t0

    async def test_concurrent_register_keeps_dedupe(self):
        import tempfile
        # 2 real processes competing for the same sid: ONE wins
        d2 = tempfile.mkdtemp(prefix="zcworkers-race-")
        p = os.path.join(d2, "bot-workers.json")
        zc_state.AtomicStore(p).write({})
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from zcode_bot import concurrency, state;"
            "r = concurrency.WorkersRegistry(%r);"
            "print(1 if r.register('sess_X', {'titulo': 'r'}) else 0)"  # noqa: spanish — legacy persisted shape inside the fixture snippet
            % (os.path.join(TESTS_DIR, "..", "src"), p))
        procs = [subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE) for _ in range(4)]
        outs = [int(pr.communicate(timeout=30)[0].strip()) for pr in procs]
        self.assertEqual(sum(outs), 1)  # exactly ONE winner
        # the file is left with ONE entry (the winner's); the children's
        # pids are already dead, so a new reader PRUNES them (a crash
        # without finally does not leave dedup broken)
        raw = json.load(open(p))
        self.assertEqual(list(raw), ["sess_X"])
        self.assertEqual(len(zc_conc.WorkersRegistry(p).items()), 0)


class TestQuestionFiles(unittest.IsolatedAsyncioTestCase):
    """F16: ❓ questions via FILES across processes — Go channel
    semantics (send writes, recv blocks reactively until it arrives)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zcq-")
        self.b = zc_conc.QuestionFiles(self.dir)

    async def test_ask_wait_answer_roundtrip(self):
        # the answer can arrive even BEFORE the recv arms its kqueue:
        # fswatch's safety scan rescues it — no coordination sleeps
        # (the blocking wait runs in the executor)
        self.b.ask("r1", [{"question": "Sure?"}], "T1")
        recv = asyncio.ensure_future(
            asynkit.call(self.b.wait, "r1", timeout=5, poll=0.2))
        t0 = time.time()
        self.assertTrue(self.b.answer("r1", "option 2"))
        self.assertEqual(await recv, "option 2")
        self.assertLess(time.time() - t0, 2.0)  # reactive, no long polls
        # the recv consumed its files (no garbage)
        self.assertFalse(os.listdir(self.dir))

    async def test_producers_and_consumers_scale(self):
        # N producers ask, M pool resolvers answer via the channel:
        # each answer reaches ITS question (routing by rid).
        # Raising N/M does not change the test — more coroutines in gather.
        N, M = 3, 2
        ch = asynkit.Chan()

        async def producer(i):
            rid = "r%d" % i
            self.b.ask(rid, [{"question": rid}], "t")
            await ch.put(rid)
            return await asynkit.call(self.b.wait, rid, timeout=10, poll=0.2)

        async def resolver():
            while True:
                rid = await ch.get()
                self.assertTrue(self.b.answer(rid, "ans-" + rid))
                ch.task_done()

        pool = [asyncio.ensure_future(resolver()) for _ in range(M)]
        answers = await asyncio.gather(*[producer(i) for i in range(N)])
        await ch.join()  # the channel exhausted its work: the resolvers are now extra
        for w in pool:
            w.cancel()
        await asyncio.gather(*pool, return_exceptions=True)
        self.assertEqual(answers, ["ans-r%d" % i for i in range(N)])
        self.assertFalse(os.listdir(self.dir))  # channel clean

    async def test_wait_timeout(self):
        t0 = time.time()
        self.assertIsNone(self.b.wait("r2", timeout=0.5, poll=0.1))
        self.assertGreaterEqual(time.time() - t0, 0.4)

    async def test_two_simultaneous_questions_do_not_cross(self):
        # REGRESSION of the single global slot: two questions → each
        # answer reaches ITS question
        self.b.ask("r1", [{"options": [{"value": "one"}]}], "t1")
        self.b.ask("r2", [{"options": [{"value": "two"}]}], "t2")
        self.assertTrue(self.b.any_pending())
        # answering r1 does not touch r2
        self.assertTrue(self.b.answer("r1", "one"))
        self.assertEqual(len(self.b.pendings()), 1)  # r2 still pending
        self.assertEqual(self.b.pendings()[0]["request_id"], "r2")
        self.b.pop("r2")
        self.assertFalse(self.b.any_pending())

    async def test_first_pending_by_sid(self):
        self.b.ask("r1", [{"question": "A?"}], "T1", sid=support.T1)
        self.b.ask("r2", [{"question": "B?"}], "T2", sid=support.T2)
        self.assertEqual(self.b.first_pending(support.T2)["request_id"], "r2")
        self.assertEqual(self.b.first_pending(support.T1)["request_id"], "r1")
        # sid without its own question → the oldest one
        self.assertEqual(self.b.first_pending("sess_OTRA")["request_id"], "r1")
        self.assertEqual(self.b.first_pending()["request_id"], "r1")

    async def test_answer_missing(self):
        self.assertFalse(self.b.answer("nope", "x"))

    async def test_prune_of_orphan_question(self):
        # crashed worker: its old q does not stay pending forever
        self.b.ask("vieja", [{"question": "A?"}], "T1")
        stale = self.b._p("q", "vieja")
        obj = json.load(open(stale))
        obj["t0"] = time.time() - 7200  # 2h
        json.dump(obj, open(stale, "w"))
        self.assertEqual(self.b.pendings(), [])
        self.assertFalse(self.b.any_pending())


class TestProtoClientRegresionDeadlock(unittest.IsolatedAsyncioTestCase):
    """REGRESSION of incident 2026-10-12: request() nested wlock inside
    wlock → deadlock, bot mute for 10 min. With the fake server we verify
    that CONCURRENT requests from several threads complete."""

    def _client(self):
        return zp.ProtoClient(
            argv=[sys.executable, os.path.join(TESTS_DIR, "fake_server.py")])

    async def test_concurrent_requests_complete(self):
        c = self._client()
        try:
            errors = []

            async def hammer(n):
                for i in range(6):
                    r = await asynkit.call(
                        c.request, "probe/metodo%d" % ((n * 6 + i) % 18),
                        {"slow_ms": 10}, timeout=15)
                    if r.get("error") or (r.get("result") or {}).get(
                            "echo") != "probe/metodo%d" % ((n * 6 + i) % 18):
                        errors.append(r)
            t0 = time.time()
            await asyncio.wait_for(
                asyncio.gather(*[hammer(n) for n in range(3)]), timeout=30)
            self.assertEqual(errors, [])
            self.assertLess(time.time() - t0, 20)
        finally:
            c.close()

    async def test_one_slow_request_does_not_block_the_fast_one(self):
        # determinism by SIGNAL, not by time: fake_server creates the
        # marker when the slow request is already in flight on the server
        mark = os.path.join(tempfile.mkdtemp(prefix="zcslow-"), "slow-armed")
        self.addCleanup(os.environ.pop, "ZCFAKE_SLOW_MARK", None)
        os.environ["ZCFAKE_SLOW_MARK"] = mark
        c = self._client()
        try:
            slow = asyncio.ensure_future(
                asynkit.call(c.request, "slow/one", {"slow_ms": 2000},
                             timeout=15))
            await asynkit.wait_until(
                lambda: os.path.exists(mark),
                msg="the slow request should have started on the server")
            t0 = time.time()
            r = await asynkit.call(c.request, "fast/two", {}, timeout=15)
            self.assertLess(time.time() - t0, 1.5)  # did not wait for the slow one
            self.assertEqual((r.get("result") or {}).get("echo"), "fast/two")
            slow_r = await slow
            self.assertEqual((slow_r.get("result") or {}).get("echo"),
                             "slow/one")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
