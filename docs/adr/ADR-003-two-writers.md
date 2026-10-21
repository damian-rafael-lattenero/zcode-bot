# ADR-003: Two writers (app ↔ bot) over the same sessions

**Status:** accepted (2026-10-12)

## Decision
Task OPEN in the app → its runtime owns the in-memory state until it is
reopened; task CLOSED → the state persisted on disk wins (the bot reads/
writes it headless). The bot NEVER assumes the app will see its changes
live, and tells the user when appropriate (e.g. "I reopened the task"
[«reabrí la task»]).

## Rationale
A compact from the bot over a task open in the app: the app ABSORBS it
(the next turn re-persists its in-memory copy) — the 10/11 incident.
The app does not destroy the compacted lineage on disk (verified), but
its window keeps showing the previous state until reopened.

## Consequences
- User rule: compact from where the task is open.
- The bot reports the authoritative numbers from the `compaction` part
  (preCompactTokenCount / truePostCompactTokenCount).
