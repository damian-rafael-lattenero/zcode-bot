"""fswatch — REACTIVE waiting for file/directory changes (F16).

Pattern: event-driven I/O multiplexing (kqueue EVFILT_VNODE, stdlib
select on macOS/BSD). kevent() BLOCKS in the kernel without burning CPU:
the thread wakes up exactly when something changes — no periodic polling,
no busy-wait. Portable fallback (CI Linux, platforms without kqueue):
sleep-based polling — the thread ALSO sleeps between checks, same
interface, worse latency.

kqueue notes (verified against the Darwin API):
- VNODE on a DIRECTORY fires for create/delete/rename INSIDE it, not for
  appends to existing files → for 'a-<rid>.json appeared' watching the
  dir is enough; for 'the log grew' you must watch the FILE.
- KQ_EV_CLEAR: the event re-arms itself.
- Arming race: a change BETWEEN the baseline snapshot and the kqueue
  registration fires no kevent at all. The SAFETY SCAN closes it: after
  every eventless control slice the current state is compared against
  the baseline (_snapshot) — the early change is rescued within ≤1 slice
  instead of being lost until the timeout.
- Each call arms its own kqueue and closes it: no shared state.
"""
import os
import select
import threading
import time
from typing import Callable, Optional


def kqueue_available():
    return hasattr(select, "kqueue")


def _snapshot(paths):
    """Cheap fingerprint of the paths' state (stat + listdir): used by the
    kqueue safety scan and the polling fallback. A path that does not
    exist today enters as None — when it appears, the fingerprint changes."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            if os.path.isdir(p):
                out.append((st.st_mtime_ns, st.st_ino,
                            tuple(sorted(os.listdir(p)))))
            else:
                out.append((st.st_mtime_ns, st.st_ino, st.st_size))
        except OSError:
            out.append(None)
    return out


def wait_change(paths, timeout, poll=0.5, debounce=0.15,
                on_baseline: Optional[Callable[[], None]] = None,
                on_armed: Optional[Callable[[], None]] = None):
    """Blocks until something in paths (dirs or files) changes or the
    timeout expires. True if there was a change. With kqueue: reactive
    (~0 latency, zero CPU while waiting, immune to the arming race).
    Without kqueue: polls every `poll` s (sleep between checks — the
    thread doesn't burn CPU).

    Optional hooks for deterministic tests:
    - on_baseline: called right after taking the baseline snapshot
    - on_armed: called right after registering the kqueue
    """
    paths = [p for p in paths if p]
    if not paths:
        time.sleep(min(timeout, poll))
        return False
    if kqueue_available():
        return _wait_kqueue(paths, timeout, debounce, poll, on_baseline, on_armed)
    return _wait_poll(paths, timeout, poll)


def wait_for(pred, paths, timeout, poll=2.0):
    """The canonical REACTIVE+TTL wait for a condition on files: checks
    pred() and, if not satisfied, sleeps in the kernel (kqueue) until
    paths change — zero sleep-based polling. The ≤poll slice exists in
    case an append doesn't fire the file's vnode. True if pred returned
    True before the TTL. It is the conditional recv(): the waiter does
    NOT spin."""
    deadline = time.time() + timeout
    while not pred():
        rest = deadline - time.time()
        if rest <= 0:
            return False
        wait_change(paths, timeout=min(rest, poll))
    return True


def _wait_kqueue(paths, timeout, debounce, poll=0.5,
                 on_baseline: Optional[Callable[[], None]] = None,
                 on_armed: Optional[Callable[[], None]] = None):
    fds = []
    kq = select.kqueue()
    try:
        base = _snapshot(paths)  # BEFORE arming: nothing earlier is lost
        if on_baseline:
            on_baseline()
        targets = []
        for p in paths:
            try:
                fd = os.open(p, os.O_RDONLY)  # dir or file
            except OSError:
                continue  # e.g. today's log doesn't exist yet
            fds.append(fd)
            targets.append(select.kevent(
                fd, select.KQ_FILTER_VNODE,
                select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND))
        if not targets:
            # nothing watchable (all paths absent): poll — if a path
            # APPEARS, its stat goes from None to data and is detected
            return _wait_poll(paths, timeout, poll)
        if on_armed:
            on_armed()
        deadline = time.time() + timeout
        while True:
            rest = deadline - time.time()
            if rest <= 0:
                return _snapshot(paths) != base  # last rescue
            # short slice: the event wakes instantly; with no events,
            # the safety scan rescues anything that predated arming
            events = kq.control(targets, 1, min(rest, max(poll, 0.05)))
            if events:
                time.sleep(debounce)  # coalesce bursts of writes
                return True
            if _snapshot(paths) != base:
                return True
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        kq.close()


def _wait_poll(paths, timeout, poll):
    base = _snapshot(paths)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        if _snapshot(paths) != base:
            return True
    return False
