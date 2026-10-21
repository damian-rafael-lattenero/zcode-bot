"""state.AtomicStore — Atomic Write pattern (tmp + os.replace + fsync).

Reference: the rename(2) atomicity guarantee in POSIX — standard
practice for persistent state (sqlite, systemd, etc. use it). A crash
mid-write NEVER leaves a truncated JSON: the previous file remains
complete.

For files touched by MULTIPLE PROCESSES (hooks + poller, e.g. outbox),
update() additionally takes a file-lock (fcntl.flock) on a sidecar
.lock: read-modify-write without losing concurrent writes.

Internal rule (lesson from the 2026-10-12 deadlock): the lock IS
reentrant (RLock) and public methods never nest non-reentrant
acquisitions.
"""
import fcntl
import json
import os
import threading
import time


class AtomicStore:
    """State JSON file with atomic writes and a per-thread lock.

    Formats 100% compatible with the bot's current files
    (bot-session.json, bot-queue.json, outbox.json, etc.).
    """

    # F8 (15:47 incident): flock is NOT reentrant across fds — RLock is
    # within a process, but nesting update_cross_process on the SAME
    # file from the same thread opens a second fd whose flock(LOCK_EX)
    # blocks forever against the first (self-deadlock: the outbox flush
    # called send inside the lock and the failed send re-enqueued with
    # ANOTHER flock → poller frozen 30+ min). Besides the fix in
    # outbox_flush (sends OUTSIDE the lock), the flock now has a
    # DEADLINE: if it can't be acquired in time, OSError with the file
    # name — a future deadlock degrades with a log, never freezes a
    # whole process.
    FLOCK_TIMEOUT_S = 10.0

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()

    # ---------- read ----------

    def read(self, default=None):
        with self._lock:
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (OSError, ValueError):
                return default

    # ---------- write ----------

    def write(self, data):
        with self._lock:
            self._write_nolock(data)

    def _write_nolock(self, data):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ---------- read-modify-write ----------

    def update(self, fn, default):
        """Reads, applies fn(data) -> new data, writes. Atomic within a process."""
        with self._lock:
            cur = self.read(default)
            new = fn(cur if cur is not None else default)
            if new is not None:
                self._write_nolock(new)
            return new

    def update_cross_process(self, fn, default, timeout=None):
        """update() + file-lock for multi-process (hooks vs poller).
        Waits for the lock with a deadline (no eternal block: 15/10
        incident — a nested flock froze the poller for 30 min)."""
        lock_path = self.path + ".lock"
        t0 = time.time()
        left = self.FLOCK_TIMEOUT_S if timeout is None else timeout
        with self._lock:
            with open(lock_path, "a+") as lf:
                while True:
                    try:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.time() - t0 >= left:
                            raise OSError(
                                "flock timeout (%.0fs): %s — lock held by "
                                "another thread/process (possible nesting)"
                                % (left, lock_path))
                        time.sleep(0.1)
                try:
                    return self.update(fn, default)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)