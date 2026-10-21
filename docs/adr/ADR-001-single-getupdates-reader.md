# ADR-001: A single getUpdates reader per bot

**Status:** accepted (2026-10-12) · **Context:** bot @glm_lattenero_2_bot

## Decision
The poller (LaunchAgent com.zcode.telegram-cmds) is the ONLY consumer of
the 2_bot's getUpdates. The hook processes waiting for replies (permissions 🔐,
questions ❓) coordinate through the `state/tg-busy` flag (mtime): while
it is fresh, the poller does not call getUpdates.

## Rationale
- Official: getUpdates "will not work if an outgoing webhook is set up" and two
  simultaneous consumers clash (409 Conflict) — validated empirically at
  the start of the project against the integrated bot. See docs/REFERENCES.md #2.
- The offset is shared (state/tg-offset): two readers steal updates from
  each other.

## Consequences
- The flag window must cover the WHOLE wait: it derives from wait_timeout_s+60
  (fix D2; before, a fixed 120 with 240 s waits → race).
- The integrated bot (@glm_lattenero_bot) uses its own token: never cross
  them.
