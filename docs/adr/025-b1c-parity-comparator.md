# ADR-025: B1c — the parity comparator and the cutover gate

Date: 2026-10-29
Status: deployed; companion of ADR-023/024 (the shadow watchers this
gate judges).

## Context

Shadow mode only matters if something measurable decides when the
shadow has earned the real work. B1c is that judge: a comparator that
differs the Kotlin watchers' decisions against what the Python bot
actually did, daily, with a verdict — and a week-long gate that unlocks
the cutover.

## Decision

**Truth sources (the Python side, read-only):** `kb.log` for zconn —
two-line records (leveled header with explicit offset, then the bare
`zconn down`/`zconn up` message) — and `prompt-worker.log` (pwlog) for
the spectator — `spectator: turn pinned <turnId>` paired with
`spectator end: steps=N`. Both formats are pinned by tests against
production lines. The Kotlin side is the shadow JSONL (ShadowDecision
moved to contract-types: one schema, writer and reader).

**Gate semantics:**

- *Coverage clipping*: Python events older than the daemon's first word
  are history (`beforeCoverage`), not divergence — the shadow cannot
  have seen them.
- *zconn*: the two transition streams must match kind-for-kind with
  each pair within 60 s (each side polls the same log at its own
  cadence; the drift is poll latency, and the report tracks it).
- *spectator*: every Python turn with steps > 0 must have a shadow turn
  starting within 90 s with the SAME step count. Turns with steps=0 are
  skipped: both sides are silent by design (Python sends nothing; the
  shadow records nothing).
- *Verdicts*: PARITY (every overlapping event agrees), DIVERGENT
  (anything unmatched), INSUFFICIENT (nothing overlapping yet).
- *The week*: `comparator --week` aggregates the daily reports — seven
  consecutive PARITY days earn the cutover knob. Rollback is the knob
  back. Text-level parity is already pinned by the golden vectors; a
  Telethon reader (T6) is the future upgrade that would diff the actual
  Telegram texts too.

**The first catch (its own proof of work):** the comparator's first
real run, hours after the shadow's birth, flagged DIVERGENT — the
orchestrator was spawning spectators for the bot's own phone sessions
where Python spawns none, because the hook's spawn condition has THREE
gates (`mac_spectator` knob, `_task_current_ok` — only the FOCUSED task
gets a spectator — and single-flight) and the shadow applied only the
worker check at spawn time (the focus gate was applied per message,
which is Python's SEND-time gate, not its spawn gate). Fixed: the
orchestrator now applies the hook's gates in its order; the pre-fix
decisions are archived under `shadow-archive/`; the parity window
restarted clean at 2026-10-29 12:37 with verdict INSUFFICIENT — the
honest state while evidence accumulates.

## Verification

Suite: 88 tests across four modules (the comparator's parsers and
matchers tested against the real log formats), detekt clean, CI green.
Daily cadence: run the comparator by hand or from cron after real
traffic; reports land in `~/.zcode/bot-kotlin/parity/`.
