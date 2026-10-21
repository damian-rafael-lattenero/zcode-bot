# ADR-007 — Turn trace: steps + full CoT via a Telegram document

**Status:** accepted (2026-10-12)
**Context:** Damian wants to read the COMPLETE step-by-step of every turn
(reasoning from start to finish, commands, files) by scrolling in the
chat. The live view edits ONE message with the tail — everything before
it is visually "lost" — and a turn's CoT does not fit in 4096-char
messages (a real turn: 115K chars of reasoning, 49 tool calls).

## Facts that make it possible

- Every step of the turn persists as an assistant message with parts
  `step-start / reasoning / text / tool / step-finish` (verified live
  against the history of a real session). The FULL CoT is on disk.
- `session/messages {afterMessageId}` delivers the steps as they
  complete → the 🔧 steps can be shown DURING the turn.
- Reading the history DOES cross runtimes (halting does not, ADR-002):
  /cot also works for tasks run in the Mac app.
- `sendDocument` (Bot API) accepts .txt via multipart/form-data — the
  Telegram viewer scrolls it whole; 4096 chars stops being a limit.

## Decision

1. **During the turn** (live view v2 — `_live_view` then, `LiveTurnView`
   today): a «🔧 steps» message that grows (poll afterMessageId every
   6 s, last 12 visible) + the reasoning tail widened to 3200 chars.
   Zero extra messages per step (rate limits: it edits, it does not
   send).
2. **Incremental CoT in the chat** (Damian's explicit request, v2): the
   live tail is ONE editable box; when the 4096 chars/message limit is
   hit, the excess goes out as PERMANENT «🧠 CoT N» messages of ~3000
   chars (chunker `_cot_chunks_to_send`, 1.2 s pause between sends due
   to the FAQ's ~1 msg/s per chat). The reasoning stays scrollable from
   start to finish WHILE the turn runs. Cap of 30 boxes/turn with a
   notice (the document brings the rest). Config `cot_stream=false`
   reverts to tail-only behavior.
3. **At the end** (`_turn_trace`): a .txt document with numbered STEPS +
   FULL REASONING + ANSWER, authoritative from the history (live buffers
   only as fallback). Persisted in `state/last-turn-trace.txt`. Config
   `trace_documents=false` turns off the document (the file stays).
4. **`/cot [n|@sid]`** (alias /trace): on-demand trace of any task (last
   ~150 messages) — also for runs in the app.

## Consequences

- The full CoT is ALWAYS available (disk + document), even if the live
  streaming failed or the task ran on the Mac.
- Discarded alternative: chunk-flush of live messages (the CoT in
  3900-char messages) — 115K chars = ~30 messages per turn: too much
  noise and rate-limit risk; the document replaces it.
- The turn trace reflects what was persisted: for tasks open in the app
  the two-writers divergence applies (ADR-003).
