# ADR-013 — Live action: live tools via incremental polling

Date: 2026-10-15 · Status: accepted · Supersedes: nothing (extends F22bis)

## Context

The bot already showed the live CoT (deltas from `session/events`) and
one box per COMPLETED tool (F22bis, `session/messages` poll every 6 s).
Damian asked to see "everything I do here, but on the phone" [«todo lo
mismo que hago acá, pero en el celu»]: the tool calls, the terminal
commands and the diffs, **as they happen** — a live action of the
conversation.

Two underlying problems:

1. **Latency**: a tool appeared only once completed (+6 s of poll).
2. **The event poll was not incremental**: the server resends the
   COMPLETE replay on every call (the schema rejects `afterEventId` —
   ZodError, verified). A real turn over a large session measured
   `evs=815 frame=24MB rss=3160MB cad=5s`: 1 MB away from tripping the
   anti-OOM guard that turns the live CoT off.

## Decision

**Consume the tool lifecycle from the event stream, with incremental
polling by `afterSeq`.** The evidence is a spike against the real
app-server (`scripts/spike_f7a.py`):

- The stream (`session/events`, wrapper `model.streaming`/`tool.updated`)
  brings: `tool_input_start{toolCallId, toolName}` →
  `tool_input_delta{delta}` (the JSON input **being typed**) →
  `tool_call{input ALREADY parsed}` → `tool.updated.started` →
  `tool.updated.result{success, content}`.
- `afterSeq` IS in the schema and filters `seq > afterSeq` (verified:
  `afterSeq=32 → 0 events`). `seq` is a dense per-session counter.
- `session/subscribe` returns `eventSeq` — an initial cursor that skips
  the startup replay.
- `session/messages` exposes `part.callID`, the same key as
  `toolCallId`: exact stream ↔ history pairing.

### Discarded alternatives

- **Capturing `session/event` pushes in ProtoClient** (the spike's plan
  B): incremental polling gives the same information with ≤2 s latency
  without touching the push reader (which discards params on purpose:
  the resume's `session/update` frames bring MBs). Re-evaluate if the
  latency ever bothers.
- **Keeping the full replay + dedupe**: that is the status quo that
  inflated the RSS to 3.1 GB. The anti-OOM guards stay as a safety net,
  not as the normal mode.

## Implementation (F7)

- `LiveTurnView._poll_events`: request with `afterSeq=last_seq` (cursor
  = max `seq` seen; initial: the `eventSeq` of the subscribe that
  `prompt_worker` passes); dispatch of the tool kinds to `_tool_event`
  (then `_tool_evento`).
- Box per tool: born `🔵` at `tool_call` (parsed input → `_tool_box`,
  then `_tool_caja`; full command/real diff), **edited** to `✅`/`❌`
  with `tool.updated.result{content}`. The current tool shows in the
  live box (being typed / `🔵 running`).
- Fallback: if the stream did not close a box (events_off mid-tool),
  the message poll closes it with the history's output; tools with a
  live box are not duplicated (`boxed`, by `callID`). With `events_off`
  the behavior is the usual F22bis.
- Config: `tools_stream` turns off all tool live; `background` (F15,
  then `segundo_plano`) keeps silencing boxes.

## Consequences

- KB-sized frames instead of MBs; the worker's RSS stops inflating with
  the session history; the 25 MB/1200 events/2 MB guards remain as a
  net against unexpected replays.
- A tool-heavy turn sends ~1 box per tool (~1 msg/s with pacing) —
  Damian chose "all tools" knowingly; a per-tool filter is trivial if
  the volume bothers.
- Declared follow-up (out of scope): `turn.completed`/`turn.failed` and
  `permission.requested` via events — they would replace the JSONL log
  scan of `_turn_end_poll`. **Closed in F9** (ADR-015): the turn end
  goes through the stream with correlation by `inputId`; permissions
  were already push (`interaction/requestPermission`).
