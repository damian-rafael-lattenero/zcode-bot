# ADR-015 — Event-based turn end (the stream replaces the log scan)

Date: 2026-10-15 · Status: accepted · Supersedes: the follow-up declared
in ADR-013 · Decision gate: `scripts/spike_f9_turnfin.py`

## Context

`_turn_end_poll` (the thread waiting for the worker's turn end)
detected `turn.completed`/`turn.failed` by scanning the CLI's JSONL logs
(512 KB tail, 1 s throttle, wakeups via fswatch). It worked, but it was
a secondary channel: extra text the CLI writes, which the worker parses
in excess, on every round of the turn. ADR-013 had left it declared as
a follow-up: "via events — they would replace the JSONL log scan".

## Decision

**The turn end is consumed from the `session/events` stream, with the
log scan as a permanent fallback.** Spike evidence:

- The stream brings the TURN lifecycle with `type` `turn.started`
  (payload: `inputId`, `input`, `executionStartedAt` in ms) and
  `turn.completed`/`turn.failed` (payload: `inputId`, `resultType`,
  `response`, `usage`, `duration`).
- **The stream does NOT bring turnId** (the logs' correlation key): the
  F20 correlation is redone with `inputId`, shared between the
  `turn.started` and its end. The first `turn.started` after the
  `session/send` sets `mine`; only the end with THAT inputId counts (a
  foreign `completed` without its own `mine` is never accepted, per
  `t_send`).
- The buffer does NOT repeat old events to a new subscriber:
  `subscribe` returns `eventSeq` (cursor) and `afterSeq` filters
  `seq > cursor` — incremental polling in KBs, the same anti-OOM
  discipline of ADR-013.

## Implementation (F9)

- `live_view._stream_end` (then `_fin_de_stream`): one stream poll →
  `(end, mine, last_seq)`; it normalizes to the logs' shape
  (`{"event": ..., "error": {...}}`) so that no consumer changes.
- `_turn_end_poll(conn=, start_seq=)`: with connection+cursor it goes
  by stream; any failure (request in error, dead conn, frame >25 MB =
  broken replay) degrades 1:1 to the log path, which remains the ONLY
  path of the orphan supervisor (it sees turns of already-dead workers,
  with no connection) and of the no-subscribe mode.
- `workers._run_turn` (then `_correr_turno`): the `subscribe` happens
  BEFORE the `send`, ALWAYS (before it was only for the live view and
  after the send) — the cursor guarantees the turn's events enter the
  stream. The two `_turn_end_poll`s of the rotation (summary and seed)
  go through the same path.
- `permission.requested`: unchanged and on purpose — it was already
  push (server→client `interaction/requestPermission` → the
  ProtoClient's `on_permission` callback). It does not appear as a kind
  in the stream.
- `zcode_proto.py` untouched: the push reader keeps discarding params
  on purpose (the resume's `session/update` frames bring MBs).

## Consequences

- The turn end stops depending on the CLI writing its log (and on the
  scan's throttle): it arrives through the same channel as the rest of
  the turn.
- The log fallback stays as a net, not as the normal mode — if the
  stream fails the end is NEVER lost: it degrades and continues.
- A stream `turn.completed` brings `resultType` → a turn ending
  "completed" with an error maps to an honest `turn.failed`.
- Tests: `tests/test_f9_eventos.py` (correlation, foreign peer,
  degradations).
