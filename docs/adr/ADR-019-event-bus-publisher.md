# ADR-019 — The event bus publisher: best-effort JSONL, one owner per turn

**Status:** accepted (2026-10-28). Context: agent-brain (a separate
repo, Kotlin) needs every turn and tool call of this bot and of the
Mac's own sessions, to index them (RAG), to extract memories from them
and to evaluate them. The bot is standard-library only (ADR-010) and the
harness fork stays untouched; neither can carry a Kafka or HTTP client
for this. There is a precedent: `on_user_prompt` already appends a turn
event to `~/.zcode/diff-ask/turn-events.jsonl` for the diff-ask plugin.

## Decision

1. **A file is the bus.** `bus.py` appends one JSON line per event to
   `config.BUS_FILE` (`~/.zcode/agent-brain/bus-events.jsonl`).
   Contract: `{"id","ts","kind","sid","turn","source","payload"}` with
   `kind` in `user.prompt | turn.started | tool.call | tool.result |
   turn.completed | turn.failed`, `source` in `bot | hook`. The
   consumer (agent-brain) tails by byte offset and deduplicates by `id`,
   so delivery is at-least-once and replays are harmless.
2. **The bus never breaks a turn.** The tap points call `bus.tap()`,
   which swallows every error (an unwritable path, a bug inside the
   module itself), counts it in `bus.failures` and returns False. This
   is the live-io invariant applied to one more sidecar.
3. **Off by default.** The knob `event_bus` (DEFAULTS, False) gates
   every write: a bot without the knob behaves byte-for-byte as before.
4. **One owner per turn.** The worker publishes its phone turns from
   `_run_turn` (`turn.started` after the send, `turn.completed` /
   `turn.failed` at the end, under `turn = bus.turn_id(t0)`) and
   `LiveTurnView._bus_tool` publishes `tool.call` / `tool.result` under
   the same id. The hooks publish the Mac's turns (`user.prompt` in
   `on_user_prompt` under the marker token, `turn.completed` in
   `on_stop` reading that token back) **only when no worker owns the
   session**: the hooks fire for the bot's headless turns too and would
   otherwise double every phone turn.
5. **Lines stay small.** Free text (prompt, reply, tool input) is
   clipped to `PAYLOAD_TEXT_CAP` (4000) so one append is one write and
   two processes appending at once cannot interleave inside a line.

## Consequences

- Nothing in the bot depends on the consumer existing; the file grows
  until agent-brain rotates it (its job, not ours).
- `tests/test_bus.py` (13): contract line, clipping, stable turn id,
  programmer error vs runtime failure, concurrent appends, the hook
  pair under one id, the headless notice peeled off, worker-owned
  sessions silent in the hooks, the golden "a bus failure never breaks
  the hook", and the REAL prompt_worker turn on the fake runtime
  published start to end under one id (plus: without the knob, no
  trace).
- `_stream_end` was considered as a tap and rejected: it is a pure
  function without the session at hand; `_run_turn` owns the terminal
  event.
