# ADR-016 — Auditor: invariant observer across components

**Status:** accepted (2026-10-15, F30, commit after b74aac0)
**Trigger:** the 10/15 18:10-18:40 incident — 4 `/compact` and 2 turns
failed in a loop with the same error and nobody correlated them; Damian
detected it by hand, an hour and a half later, with a `/nuclear` in
between.

## Context

The bot has **intra**-component self-checks: `_orphans_check` (orphan
turns), stuck-watch (stuck session), outbox (dropped sends),
housekeeping (purge), `/nuclear` (reset with verdict). What it does not
have is anybody watching the **boundaries**: states diverging between
components, repeated failures forming a pattern, ownerless processes.
Every big incident of 10/15 was a boundary impurity:

| Incident | Impurity | Who saw it |
|---|---|---|
| 15:47 429+deadlock | burst of correlated send failures | nobody (Damian) |
| 15:40 foreign pop | registry ≠ live processes | nobody (manual diagnosis) |
| 18:10 timeouts | same failure ×6 in 30 min | nobody (Damian on the 4th) |
| 18:11 nuclear | orphaned stop marker | nobody (bounce at 18:13) |
| 18:10 zombie | Popen without wait | nobody (declared debt) |

## Decision

1. **Shared failure log** (`audit.py` → `state/bot-audit.json` — then
   `state/bot-auditoria.json`; flock, cap 300): one line per failure
   `(ts, sid, type, detail)` with `registrar()` that **never raises** —
   it lives on error paths and cannot make them worse. Minimal deps
   (config + state, no core) because `utils/http.py` imports it and
   http imports it everywhere: a cycle here would be systemic (gated by
   test).
2. **Auditor in the poller's supervisor** (v1): runs in both drivers
   (`AsyncPoller` and `supervisor_loop`) as an injected dep, with its
   own throttle (`auditor_interval_s`, 0 = off; then
   `auditor_cada_s`). Every invariant is best-effort: one broken, the
   others still run; none raises into the supervisor.
3. **Philosophy**: it HEALS what is idempotent (consume an orphaned
   marker); it RECOMMENDS what depends on state (sick session → rotate
   or /task; ghost → /stop go; queue → /qgo). It never runs destructive
   actions on its own.
4. **Anti-spam**: keyed alerts (`failures|<sid>|<type>`, `ghost|<pids>`,
   `idle-queue|<sid>` — then `fallos|…`, `cola|<sid>`) with per-key
   cooldown (`auditor_alert_cooldown_min`, default 60) — the mark lives
   in the log itself (type=alert), not in memory: it survives reloads.
5. **Zombie reap**: `_SPAWNED` + `_reap_children()` (then
   `_reapar_hijos()`) in `_orphans_check` (`Popen.poll`, waitpid
   WNOHANG) — the poller reaps its dead children every cycle.

## Consequences

- Pattern detection on the **second** attempt (vs. Damian's fourth by
  hand), and visibility of divergences that no individual log shows.
- The log is new shared state: cap 300 and lowercase entries — the
  file does not grow without bound.
- The knobs (`auditor_interval_s` / `auditor_failures_min` /
  `auditor_alert_cooldown_min`) are hot config like the whole bot;
  turning it off is `auditor_interval_s: 0`.
- **Honest v1 limitation**: it lives in the poller — a frozen poller
  freezes it. Mitigated by the flock deadline of e4d72fd (the poller
  can no longer freeze forever). If it ever happens again, v2 is the
  same auditor as a separate launchd service (the watch-app pattern),
  immune to the poller: +1 service, same module.

## What it is NOT

- Not an intelligent LLM/agent: it is a deterministic invariant check
  over files.
- It does not replace _orphans_check/stuck-watch: it complements them
  (they see inside each component; the auditor, the seams).
- It does not measure protocol contracts (durations, response sizes):
  that is covered by F29's adaptive timeouts and their pwlogs.
