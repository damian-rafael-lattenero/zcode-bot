# ADR-008 — Memory and speed optimization: patterns applied and discarded

**Status:** accepted (2026-10-13)
**Context:** after the real OOM incident (poller at 2.9 GB → macOS
SIGKILL → lost turn), Damian asked to audit the code against the known
space/speed optimization patterns and decide which ones fit. The
project's guiding principle: named pattern + source, nothing invented.

## Applied (pattern → where → what it protects)

1. **Circuit Breaker** (learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker)
   → `EVENTS_TRIP` in the live view (`_live_view` then, `LiveTurnView`
   today): if the `session/events` replay exceeds 1200 events, the live
   stream turns OFF for the rest of the turn and the box shows honest
   state (degradation, not death). Prevents repeating an expensive
   operation that had already proven lethal.
2. **Bulkhead / pool isolation**
   (learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead)
   → `ctx_probe.py` as a throwaway subprocess: a giant session resume
   inflates THAT process, never the poller. Same spirit: the workers
   live in threads separate from the command loop. (Retired in F31,
   10/16: the runtime does not persist the projection — `contextUsed`
   was always 0 — and /ctx now reads the rollout's last usage, with no
   resume. The workers remain the living bulkhead.)
3. **Release of the consumed prefix** (consumer-offset semantics —
   Kafka docs: the consumer advances its offset and the log retains what
   was already consumed) → `cot_flush` (today `LiveTurnView._cot_flush`):
   `cot_full = cot_full[nf:]` after sending each box. What was already
   delivered to the chat is NOT retained in memory.
4. **Sliding window / bounded buffers** → `evs[-400:]` (observation
   window), 3200-char tails in the live box, `tail_bytes=LOG_TAIL`
   (512 KB) in `_turn_end_poll` (read only the file's tail, no slurp).
5. **Dedupe cache + skip of large payloads** → `seen` (then `vistos`)
   by eventId + discard of events >30 KB (snapshots): what can
   accumulate never grows with payload size.
6. **Retry with backoff + rate limiting** (Telegram FAQ ~1 msg/s per
   chat; 0/1/3 retries in `send`, 5 s sleep on 429, 1.2 s pause between
   CoT boxes, edits ≥3 s) → slow progression, never a burst.
7. **Tail-following with generators** → `cli_logs.iter_events` (yield
   per event, needle-filter before json.loads) and the 512 KB log tail.
8. **Connection pool per unit of work** → ONE `ProtoClient` per worker
   (not per request); closed in `finally` (RAII, ADR of proto.py).

## Audited and DISCARDED (with reason)

- **`"".join(list)` instead of `+=` to accumulate deltas**: in CPython,
  `str += x` with the only reference is amortized O(1) (a documented
  interpreter optimization — bugs.python.org/issue980695; PyPy does not
  have it, pypy.org/posts/2023/01/string-concatenation-quadratic.html,
  but we ran system CPython 3.9 then — 3.13 today, ADR-010). Our buffers
  (reasoning/text/cot_full) are locals with refcount 1 → switching to
  join adds complexity without gain.
- **O(1) watermark instead of the `seen` set**: eventIds are not
  guaranteed monotonic (defensive dedupe against replay); the set holds
  small strings (MBs at most in huge sessions) — not worth the risk of
  losing deltas.
- **TTL cache of /ctx results**: the better alternative already exists
  (subprocess probe, bulkhead); caching in the poller reintroduces the
  risk of retaining payloads.
- **On-disk state compression**: state files are KBs — complexity cost >
  benefit.
- **asyncio / dedicated I/O threads**: stdlib-only (then) + the poller's
  10 s cycle; the real I/O (Telegram) is already ~1/s. Rewriting for
  "speed" that is not the bottleneck. (Later superseded for the webhook
  transport by aiohttp — ADR-010/ADR-011.)

## The root cause of the OOM (lesson for the future)

`session/events` returns the session's COMPLETE replay (no cursor, Zod
strict — re-proven). The cost was not retention (dedupe worked): it was
TRANSIENT — json.loads of the whole replay every 2 s; pymalloc grows
arenas and does not return them to the OS → ru_maxrss climbs and macOS
kills. That is why the correct fix is not asking for it (circuit
breaker), not "freeing better".

## Verification

`ru_maxrss` is logged at every turn end (pwlog "end: ... rss=%dMB"); the
idle poller today: ~20 MB. Living regression: if a "testing" turn ever
reports >1 GB again, the trip did not reach → lower EVENTS_TRIP.
