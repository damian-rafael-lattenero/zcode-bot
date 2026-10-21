# ADR-017 — Spectator: the Mac's turn streams via the rollout, not the bus

**Status:** accepted (2026-10-19). Damian's request: see from the phone
what a turn run in the Mac app is doing (like the phone turns: tools,
diffs, CoT). F32 covers start and end; F33 covers the middle.

## Context — the bus is closed (10/19 spike)

The «natural» path was subscribing to the session's stream from a
spectator client (spectator — then espectador). Verified LIVE against
an in-flight app turn:

1. `session/subscribe` (deliveryKind desktop-continuous) on the app's
   session → **`"Session is not active"` (error -32004)**.
2. `session/events` from a fresh client → 0 events, while the turn was
   producing steps at that very moment.
3. The code already knew: live_view.py (the «no deltas» branch): «its
   internal streaming does not cross to the bot» [«su streaming interno
   no cruza al bot»] (F7 observation).

The event bus belongs to the client that DRIVES the turn. A spectator
cannot hang from it.

## Decision

**Incremental tail of the rollout**
`~/.zcode/cli/rollout/model-io-<sid>.jsonl` — the same file F31 reads
for /ctx. Verified that it records EVERY turn (app or headless) with
per-completed-request granularity: `response.toolCalls` (name + full
input → command/diff), `response.text`, `finishReason`,
`startedAt/completedAt`, `turnId`, `querySource`. It is pure file: the
spectator creates no runtime and no protocol client — lighter than a
worker (one python process reading).

- **Read**: own offset; a final line without `\n` is not consumed (it
  may be mid-write); a record > 8 MB is skipped (the offset advances
  anyway); the whole file is never read.
- **Reactive wait**: `fswatch.wait_change` on the rollout + the turn's
  marker (kqueue, zero busy-wait — house rule).
- **F20 correlation**: the first record with `startedAt ≥ t0` sets THE
  turnId; other turnIds and `querySource != main_turn` (compact) do not
  exist. Avoids delivering crossed turns in long sessions.
- **End**: marker consumed (on_stop removes it → F32 delivers the final
  answer) or hard cap 35 min. The spectator NEVER sends the final text
  (`finishReason == "tool-calls"` only) — non-duplication is by
  construction, not by race coordination.
- **Gates**: `mac_spectator` (then `mac_espectador`; master, default
  on), `tools_stream` + `tools_box_filter` (then `tools_caja_filtro`;
  boxes), F15 (a session with a worker does not spawn: the worker
  already streams its own), `_task_current_ok` (the active task only).

## Consequences

- Per-STEP granularity (the record is written when the request
  completes), not per delta: there is no «live typing» as in headless
  turns.
- **CoT not available for app turns**: the runtime does not persist the
  thinking blocks — neither in the bus (closed) nor in the rollout (the
  next record's request comes without them: the runtime drops them from
  the history for this provider). If they ever appear, `_process` (then
  `_procesa`) is the place.
- Cost: one python process during the Mac's turns (file reading +
  sends). No runtime, no protocol memory.
- Alternative not adopted: ZCode's OFFICIAL mobile remote control (new
  version: account login + device management) covers «watching from
  the phone» via the Z.ai app. Telegram stays: no third-party account,
  unified history, and the bot's whole control plane.
