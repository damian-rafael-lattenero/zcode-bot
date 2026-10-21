# ADR-002: /stop halts through the worker's OWN runtime

**Status:** accepted (2026-10-12)

## Decision
`/stop` (phone-started runs only) sends `session/stop` with the ProtoClient
OF the worker running the turn (registered in `WorkersRegistry`), never
with a fresh runtime.

## Rationale
One runtime cannot see another's turns (proven live: a session running in
the app shows as "idle" to a headless runtime; 9 cross-stop methods →
-32601). A fresh runtime cannot reach the turn and, on top of that, it
misdiagnosed it as an «app turn» [«turno de la app»]. REFERENCES.md #12.

## Consequences
- Requires a thread-safe ProtoClient (concurrent requests): wlock
  serializes id-alloc + writes to stdin; `Lock` is NOT reentrant — never
  nest it (10/12 deadlock, regression-tested with a concurrent fake server).
- App turns: outside /stop's scope (they are halted with the app's Stop
  button; synthetic input on macOS requires TCC, there is no path without
  permissions).
