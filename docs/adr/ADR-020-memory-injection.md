# ADR-020 — Memory injection: bounded, cited, best effort, never on the bus

**Status:** accepted (2026-10-28). Context: agent-brain (P3) extracts
durable memories (facts, decisions, preferences) from every completed turn
it sees on the bus and serves them back over HTTP (`/memory/search`). The
bot is the natural place to USE them: a phone turn that already knows
"this user wants Corretto JDKs through brew casks" does not ask again.

## Decision

1. **A knob, off by default.** `memory_inject` (DEFAULTS, False). With it
   on, `workers._run_turn` asks the brain for the `memory_inject_k` (3)
   memories closest to the prompt and prepends them as a block:
   ```
   [memory · agent-brain · 2 item(s) · ids 12,40]
   - preference #12: Prefers Amazon Corretto JDKs via brew casks.
   - decision #40: The core is Kotlin, not Python.
   —
   <the user's text>
   ```
   The headless guardrail (F4d) still closes the prompt.
2. **Bounded.** At most `memory_inject_k` items and
   `memory_inject_max_chars` (1200) characters; an item that would blow
   the cap is dropped together with the ones after it.
3. **Cited.** Every line carries the memory's kind and id, and the ids
   travel in the bus event: `turn.started.payload.memory_ids`. Provenance
   is a query away, in both directions.
4. **Best effort.** `brain_client.memory_context` returns `("", [])` when
   the knob is off, the prompt is blank, the brain is unreachable or
   answers nothing. A dead brain costs the turn exactly nothing (the
   live-io invariant, again).
5. **The bus keeps the ORIGINAL prompt.** `turn.started` publishes the
   user's text, not the injected prompt. Otherwise the memory worker would
   extract memories from its own output on the next turn and the store
   would compound.
6. **Read side in the chat.** `/memory <query>`, `/memory recent`,
   `/memory status` (worker counters, chat model, breaker state) through
   the same `brain_client` seam as `/brain`.

## Consequences

- `tests/test_memory_inject.py` runs the REAL prompt_worker on the fake
  runtime: the block leads the content the runtime receives, the bus holds
  the original text and the ids, the pwlog line names the ids; a dead
  brain leaves the prompt untouched; without the knob the brain is never
  asked. `tests/test_memory_cmd.py` covers the command and the block's
  bounds.
- Memories are retrieval, not truth: the block says who said what and
  when (ids); the model decides what to do with it.
