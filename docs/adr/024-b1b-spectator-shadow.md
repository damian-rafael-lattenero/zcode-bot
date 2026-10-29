# ADR-024: B1b — the spectator in shadow, and the renderer's first proof

Date: 2026-10-29
Status: deployed (shadow); companion of ADR-023 (the shared shadow
contract) and of `docs/kap-first-migration.md` §6 (B1).

## Context

B1's second watcher is the spectator (F33/ADR-017): the Mac's turns
streamed to Telegram — intermediate narration plus one numbered box per
tool call, fed by an incremental tail of the rollout
`model-io-<sid>.jsonl`, ending when F32 consumes the turn marker. In
Python it is a detached process spawned by the `on_user_prompt` hook; it
is also the migration's first slice of the RENDER core (the boxes are
`messages._tool_box`), the subsystem B4 will rebuild on KapStream.

## Decision

The same shadow contract as ADR-023, extended to the spectator: the
daemon READS the Python bot's world (turn markers with their prompt
token, bot-workers.json, bot-session.json, agent-view pid files,
telegram-remote.json knobs with their legacy Spanish spellings, the
tasks-index sqlite read-only for titles, the rollouts) and writes ONLY
its own tree. The ORCHESTRATOR — a `Process` scanning every 2 s — makes
the hook's spawn decision (a marker for a session with no bot worker),
and each turn runs as its own `Process` whose body COMPLETES when the
marker dies (or a new token takes over, or the 35 min cap): normal
completion ends the process, so the handle map drains itself. A crashed
turn retries once, then goes mute — Python's dead spectator stays dead.

**The renderer is ported, not approximated.** `_tool_box` and its whole
primitive chain (`esc`, `preview`, `html_clip`, the F50b line stamp, the
box-filter presets) are byte-exact, and the colored diffs required a
faithful port of CPython's `difflib.SequenceMatcher` INCLUDING the
autojunk heuristic (200+ line sequences where popular lines stop
anchoring matches). All of it is pinned by 21 golden vectors generated
from the real Python into `spectator-golden.json` — the diffs and boxes
match byte for byte or the cutover never happens. Python `str` semantics
are honored where they bite: slicing by code points, the full
`splitlines` boundary set, unicode whitespace, truthiness,
`json.dumps` separators.

The push gates are evaluated LIVE per message, like Python's
`may_push`: only the stream focus may push (F15/F76), and a fresh agent
view mutes its parent (F67, 10 s freshness). Shadow decisions carry
`gated` + `sid` so the comparator (B1c) can diff exactly what Python
would have sent.

**Documented divergences** (none affects decision texts): agent-spawn
`note_spawn` is Python-internal and NOT mirrored (the box SKIP is —
that is the decision); the marker's safe name is lossy, so safe→sid
comes from the rollout listing (a collision is possible in theory,
never seen); polling (1 s) replaces fswatch (latency only); the PACE
sleep is timing, not content; `str()` of exotic JSON scalars falls back
to Kotlin rendering (never occurs in practice).

## Verification (2026-10-29, live)

Suite: 80 tests across the three modules (57 in the daemon), detekt
clean with the gate actually running, CI green. Live: agent running,
both heartbeats beating, and the orchestrator found a REAL marker
(session `...37358d65`, «zcode_bot_3» — title read from the real
tasks-index) and spawned its spectator turn with MDC `component` in the
JSON log. Shadow decisions will accumulate in
`~/.zcode/bot-kotlin/shadow/spectator.jsonl` as Mac turns happen.

Next: B1c — the comparator that diffs both watchers' shadow decisions
against what the Python bot actually sends, and the one-week parity
gate that unlocks the cutover.
