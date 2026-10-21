"""concurrency — WorkersRegistry (Registry pattern, file-backed) and
QuestionFiles (request/response correlation by id, file-based).

Golden rule (lesson from the 2026-10-12 deadlock): threading.Lock is
NOT reentrant. The locks that remain are acquired once per public
method.

F16: both contracts live in files under state/ with flock — the poller
and PROCESS-workers share the same view, and a poller restart no longer
wipes in-flight turns (the structural bug behind the «Turn was
cancelled» of 2026-10-13). Worker entries are pruned by dead PID
(os.kill(pid,0)) with an age cap against PID reuse.
"""
import json
import os
import time

from .store import AtomicStore


def _pid_alive(pid):
    """True if the process exists. PermissionError = it exists (another user)."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True  # ambiguous: don't prune (tolerable false positive)


class WorkersRegistry:
    """sid -> {t0, prompt, ws, titulo, stopped, pid} in bot-workers.json.

    File contract (stable across versions — do NOT change without
    coordinating): keys are sids, each entry with pid (owner), t0
    (epoch), stopped (bool), prompt/ws/titulo (reporting). Writes
    ALWAYS under flock (update_cross_process)."""

    MAX_EDAD_S = 4 * 3600  # anti PID-reuse: an old entry even if "alive"

    def __init__(self, path):
        self._store = AtomicStore(path)

    # ---------- prune ----------

    def _prune(self):
        """Removes dead workers' entries (crash/restart). Returns the
        live dict. No changes → no write (avoids per-hook churn)."""
        muertos = []

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            ahora = time.time()
            for k, ent in list(q.items()):
                if not isinstance(ent, dict):
                    muertos.append(k)
                    continue
                if ent.get("pid") == os.getpid():
                    continue  # workers of THIS process
                vieja = ahora - float(ent.get("t0") or 0) > self.MAX_EDAD_S
                if vieja or not _pid_alive(ent.get("pid")):
                    muertos.append(k)
            if not muertos:
                return None  # AtomicStore.update: None = no write
            for k in muertos:
                q.pop(k, None)
            return q
        self._store.update_cross_process(fn, {})
        d = self._store.read()
        return d if isinstance(d, dict) else {}

    # ---------- API (same shape as the threads era) ----------

    def register(self, key, entry):
        """True if it registered (didn't exist); False if it already did (→ enqueue)."""
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            if key in q:
                out["ok"] = False
                return None
            e = dict(entry or {})
            e.setdefault("t0", time.time())
            e["pid"] = os.getpid()
            q[key] = e
            out["ok"] = True
            return q
        self._store.update_cross_process(fn, {})
        return out.get("ok", False)

    def rekey(self, old, new):
        """Moves the entry (e.g. 'nueva' → real sid after session/create)."""
        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            ent = q.pop(old, None)
            if ent is not None and new and new not in q:
                q[new] = ent
            return q
        self._store.update_cross_process(fn, {})

    def get(self, key):
        return self._prune().get(key)

    def pop(self, key):
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            out["ent"] = q.pop(key, None)
            return q if out["ent"] is not None else None
        self._store.update_cross_process(fn, {})
        return out.get("ent")

    def pop_if_mine(self, key):
        """Pops ONLY if the entry belongs to THIS process (pid match).
        F8 (15:40 incident): the atexit of a duplicated worker (it
        enqueued and left) deleted the LIVE worker's entry — an empty
        bot-workers.json with the turn running: /stop, /status and the
        stuck-watch's F15 guard went blind. Latent since F16; the warm
        worker makes it frequent (every keepalive message goes through
        a dup). Returns True if it deleted."""
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            w = q.get(key)
            if not (isinstance(w, dict) and w.get("pid") == os.getpid()):
                out["ok"] = False
                return None  # someone else's (or missing) entry: no write
            q.pop(key, None)
            out["ok"] = True
            return q
        self._store.update_cross_process(fn, {})
        return out.get("ok", False)

    def items(self):
        """Snapshot sorted by t0 (stable /stop numbering)."""
        d = self._prune()
        return sorted(((k, v) for k, v in d.items()
                       if isinstance(v, dict)),
                      key=lambda kv: kv[1].get("t0") or 0)

    def empty(self):
        return not self.items()

    def flag_stop(self, key):
        """Marks stopped in the file and returns the titulo, or None if
        the entry doesn't exist. (F16: no longer returns client — the
        actual stopping is done by the worker with ITS runtime via the
        /stop marker.)"""
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            w = q.get(key)
            if not isinstance(w, dict):
                return None
            w = dict(w)
            w["stopped"] = True
            q[key] = w
            out["titulo"] = w.get("titulo")
            return q
        self._store.update_cross_process(fn, {})
        return out.get("titulo")

    def unflag_stop(self, key):
        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            w = q.get(key)
            if not isinstance(w, dict):
                return None
            w = dict(w)
            w["stopped"] = False
            q[key] = w
            return q
        self._store.update_cross_process(fn, {})

    def stopped(self, key):
        ent = self._prune().get(key)
        return bool(isinstance(ent, dict) and ent.get("stopped"))

    def heartbeat(self, key):
        """F8a — warm worker: refreshes t0 (against MAX_EDAD_S and so
        /stop numbers it as recent) and sets waiting=True. False if the
        entry no longer exists (something purged it: don't resurrect
        it)."""
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            w = q.get(key)
            if not isinstance(w, dict):
                out["ok"] = False
                return None
            q[key] = dict(w, t0=time.time(), waiting=True)
            out["ok"] = True
            return q
        self._store.update_cross_process(fn, {})
        return out.get("ok", False)


class QuestionFiles:
    """F16 — ❓ worker→poller questions via FILES (state/bot-q/).

    The interaction/requestUserInput callback runs in the worker's
    process; the answer comes from the poller (chat button/number). An
    in-memory threading.Event doesn't cross processes: the contract is
    q-<rid>.json (pending question) + a-<rid>.json (answer).

    - ask():    the worker writes q and builds the chat message.
    - wait():   polls a every 2 s (timeout → decline); cleans its files.
    - answer(): the poller writes a and removes q (True if there was a question).
    - pendings()/first_pending(sid): per-session routing (F15).
    - Age pruning: a q from a crashed worker doesn't live forever.
    """

    def __init__(self, dirpath):
        self.dir = dirpath

    def _p(self, kind, rid):
        safe = "".join(c if (c.isalnum() or c in "._-") else "-"
                       for c in str(rid))
        return os.path.join(self.dir, "%s-%s.json" % (kind, safe))

    def ask(self, rid, questions, titulo, sid=None):
        os.makedirs(self.dir, exist_ok=True)
        obj = {"request_id": str(rid), "questions": questions or [],
               "titulo": titulo, "sid": sid, "t0": time.time()}
        with open(self._p("q", rid), "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        return obj

    def answer(self, rid, text):
        """Writes the answer and withdraws the question. True if there was one."""
        qp = self._p("q", rid)
        if not os.path.exists(qp):
            return False
        os.makedirs(self.dir, exist_ok=True)
        with open(self._p("a", rid), "w") as f:
            json.dump({"request_id": str(rid), "answer": text}, f,
                      ensure_ascii=False)
        try:
            os.remove(qp)
        except OSError:
            pass
        return True

    def wait(self, rid, timeout=480.0, poll=2.0):
        """Blocks until the answer (returns its text) or timeout (None).

        Go-channel semantics: a BLOCKING recv that wakes when the
        message arrives — with kqueue (macOS) the thread sleeps in the
        kernel until the dir changes (zero CPU, ~0 latency); without
        kqueue, it polls every `poll` s. Always cleans its files: a
        crash on one side leaves no garbage on the other (an orphaned q
        is pruned by age in pendings())."""
        from zcode_bot import fswatch
        ap = self._p("a", rid)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(ap) as f:
                    obj = json.load(f)
                self.pop(rid)
                return obj.get("answer")
            except (OSError, ValueError):
                pass
            # reactive recv: sleep ONLY until something appears in the dir
            fswatch.wait_change([self.dir], timeout=min(poll,
                    max(0.05, deadline - time.time())))
        self.pop(rid)
        return None

    def pendings(self, max_age_s=3600):
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for n in names:
            if not (n.startswith("q-") and n.endswith(".json")):
                continue
            try:
                with open(os.path.join(self.dir, n)) as f:
                    obj = json.load(f)
                if isinstance(obj, dict) and \
                        time.time() - float(obj.get("t0") or 0) <= max_age_s:
                    out.append(obj)
            except (OSError, ValueError, TypeError):
                continue
        out.sort(key=lambda o: float(o.get("t0") or 0))
        return out

    def any_pending(self):
        return bool(self.pendings())

    def first_pending(self, sid=None):
        """F15: prefers the given session's question (the current
        task's); if none matches, the first by age."""
        pend = self.pendings()
        for q in pend:
            if sid and q.get("sid") == sid:
                return q
        return pend[0] if pend else None

    def pop(self, rid):
        for p in (self._p("q", rid), self._p("a", rid)):
            try:
                os.remove(p)
            except OSError:
                pass