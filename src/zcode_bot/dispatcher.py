"""dispatcher — the bot's ONLY update processor.

Successor of the "single getUpdates reader" (ADR-001) for the async era:
SERIAL queue + dedup by update_id + owner gate. Polling (offset confirmed
post-dispatch) and webhook (Telegram retries on non-200 → dedup is
mandatory) share THIS piece: each update is processed once, in arrival
order, one at a time — the semantics that all bot state (tg-busy, file
mailboxes, active session) has assumed since F16.

Processing is the bot's SYNCHRONOUS handler (it can block for minutes:
wait_reply, compact) → it runs in the executor (to_thread) and the loop
stays free for the webhook server and supervisor tasks.
"""
import asyncio
import collections
import time


class UpdateDispatcher:
    """Serializes updates: `submit` enqueues, `run` consumes one at a time.

    At-least-once dedup: the update_id is marked seen UPON ENQUEUE (fast
    rejection of bursts) and RELEASED if processing raised — that way a
    Telegram retry can reprocess it (duplicating beats losing, the same
    call as the 14/10 offset FIX).
    """

    def __init__(self, process, owner_ok, seen_ttl=900.0, seen_max=10000,
                 on_error=None):
        self._process = process        # (cfg, msg) -> None; sync, blocking
        self._owner_ok = owner_ok      # (cfg, msg) -> bool, fail-closed
        self._seen_ttl = seen_ttl
        self._seen_max = seen_max
        self._on_error = on_error      # exc -> None (log); None = swallow
        self._seen = collections.OrderedDict()   # update_id -> ts (monotonic)
        self._q = asyncio.Queue()
        self.accepted = 0
        self.duplicates = 0
        self.rejected = 0

    # ------------------------------------------------------------ dedup
    def _purge_expired(self):
        now = time.monotonic()
        expired = [k for k, ts in self._seen.items()
                  if now - ts > self._seen_ttl]
        for k in expired:
            del self._seen[k]

    def _mark_seen(self, update_id):
        self._purge_expired()
        self._seen[update_id] = time.monotonic()
        self._seen.move_to_end(update_id)
        while len(self._seen) > self._seen_max:
            self._seen.popitem(last=False)

    def already_seen(self, update_id):
        self._purge_expired()
        return update_id in self._seen

    # ---------------------------------------------------------- input
    async def submit(self, cfg, msg):
        """Enqueues if new and from the owner. Returns (accepted, reason)."""
        uid = msg.get("update_id")
        if uid is not None and uid in self._seen:
            self.duplicates += 1
            return False, "duplicate"
        if not self._owner_ok(cfg, msg):
            self.rejected += 1
            return False, "no-owner"
        if uid is not None:
            self._mark_seen(uid)
        self.accepted += 1
        await self._q.put((cfg, msg))
        return True, "ok"

    async def drain(self):
        """Waits for EVERYTHING enqueued to be processed (polling confirms
        the offset only here — FIX 14/10: post-dispatch)."""
        await self._q.join()

    def pending(self):
        return self._q.qsize()

    # --------------------------------------------------------- consumption
    async def run(self):
        """Serial consumer: one update at a time, in order. Never dies."""
        while True:
            cfg, msg = await self._q.get()
            uid = msg.get("update_id")
            try:
                await asyncio.to_thread(self._process, cfg, msg)
            except Exception as exc:  # noqa: BLE001 - the dispatcher must not die
                if uid is not None:
                    self._seen.pop(uid, None)  # reprocessable on retry
                if self._on_error:
                    self._on_error(exc)
            finally:
                self._q.task_done()
