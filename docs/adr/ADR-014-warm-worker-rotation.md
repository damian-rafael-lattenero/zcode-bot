# ADR-014 — Instant response: warm worker + session rotation

Date: 2026-10-15 · Status: implemented (F8)

## Context

The bot's cycle was **one process per turn**: every message spawned a
worker that started an app-server runtime and paid `session/resume`. On
a large session that resume returns the COMPLETE transcript and is used
for nothing (`_proto_ready` only checks the error): **27.5MB / 3737
messages ≈ 36.7 s measured**. The F7++ skips left the startup at one
resume (~35 s) — which grows forever with the session.

Obvious candidate discarded with evidence: **`compact` does NOT shrink
the transcript** — on 10/15 there were 4 `compact.completed` over the
F4+F7 session (10:34, 12:22, 14:39, 16:35 manual) and the resume stayed
at 27.5MB: compact shrinks the model's CONTEXT, not the history nor the
resume response.

Spike (`/tmp/spike-ws`): without a prior resume, `send`/`setModel`/
`session/messages` fail with `-32004 "Session is not active"` — the
resume is the loader, mandatory per connection. There is no way to skip
it in a fresh process.

Web (Damian's prompt: "did you search the web?" [«buscaste en la
web?»]): the protocol is the same design as the Claude Code Agent SDK.
Official sessions doc: no lightweight resume, no trimming, no
`contentProfile` for resume; `forkSession` COPIES the history. The
recommended pattern for heavy resumes is architectural: **short
sessions + seeded memory** and keeping the state alive (equivalent to
the OpenAI Responses API's `previous_response_id`: you only send the
new message). Anthropic published a postmortem (apr-2026) admitting
this same problem (trimming old thinking in idle sessions to lower
startup latency).

## Decision (Damian's choice: A + B)

1. **Warm worker** — the runtime outlives the turn: `_run_turn` (then
   `_correr_turno`; one turn over an active session, cold or warm) +
   `_warm_wait` (wait for the next prompt of THAT session: /stop
   marker, queue, heartbeat to the registry, timeout
   `warm_keepalive_s`, default 600, `0` = classic behavior). The pickup
   of the queued item runs over the SAME connection: zero resume. The
   dispatch race is already covered by the registry (message arrives →
   dup spawn → sees a live entry → enqueues → pickup ≤1 s). `/stop`
   halts in <1 s by marker (within the 4 s grace: no kill); `/stop go`
   stays available. The `waiting` index in the entry tells the waiting
   worker apart.
2. **Automatic rotation** — with the queue empty (it never blocks a
   message) and the transcript (`cli/rollout/model-io-<sid>.jsonl`)
   over `rotate_threshold_mb` (then `rotar_umbral_mb`; default 8, `0` =
   off): compact → summary turn in the compacted session →
   `session/create` on a NEW runtime → seed send with the summary →
   swap of `bot-active` + registry and queue rekey + `_restore_model`
   of the old one. The old one stays archived (reachable via `/task`);
   cold startup is back to ~5 s. Any failure → notice and continuity
   on the old session.

## Consequences

- The app's model picker can show zai-bot while the worker is warm
  (window ≤ keepalive; it already happens today during long turns).
- `/retry` during the warm says "a turn is already running" even when
  it is only waiting — accepted wart (documented).
- The warm default (600 s) comes from `config.DEFAULTS`; the tests
  override it to 0 in `support.load_module` (one single place).
- Warm turns reuse the queued item's `act` (fresh model/mode in case
  `/task` changed something while it waited).

## Out of scope

- Express path without an agent (direct API answer for simple chat).
- Centralized multi-session daemon (per-session warm achieves the same
  without breaking F16 isolation).
