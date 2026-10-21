# ADR-012: Gateway dismemberment by delegation

**Status:** accepted (2026-10-14, F4)

## Context

The monolithic gateway.py reached 4,139 lines with 10 dependency cycles
among its internal domains (reports ↔ workers ↔ hooks ↔ commands ↔ …)
and a structural fragility in the tests: ~8 files patched
`zt.send`/`zt.subprocess`/`zt.time` in the gateway's namespace — patches
that went MUTE as soon as the consuming function moved to another
module (the lesson of the "nemotron" refactor: copy-to-modularize ended
up with two divergent versions of every concept).

## Decision

1. **Move, never copy.** One single live body per concept; the
   anti-duplication gate (`scripts/check_no_duplicates.py`) runs on
   every CI. The bodies were moved TEXTUALLY; each A-commit's diff is
   pure, auditable movement.
2. **Shared seam first.** The root cut of the cycles was `core.py`
   (session state, registry accessors, stop-markers, perm-cache, queue/
   turn reads). The F1e rule extended to EVERYTHING: paths, registries
   and transport resolve by attribute at call time (`config.STATE_DIR`,
   `_workers()`, `uhttp.send`).
3. **Transport by attribute.** Modules call `uhttp.send(...)` (with
   `from zcode_bot.utils import http as uhttp`). ONE single patching
   seam for the whole suite: `mock.patch.object(zc_http, "send")`.
4. **Tests migrate with the code, same commit.** Every monkeypatch
   points at the owning module. The migration uncovered a latent bug:
   the nuclear watchdog waited 90 REAL seconds because the patched
   `zt.time` no longer reached the moved code (suite 104 s → 8.6 s).
5. **Dataclasses in a separate commit.** Movement (A) and
   representation (B) never in the same commit. Entities cross JSON on
   disk: tolerant `from_dict` (production is read at deploy), `to_dict`
   preserves the historical shape, compat mapping (get/keys/
   __getitem__) for unmigrated consumers.
6. **The extraction order respects the DAG** (an import cycle = a seam
   still to extract, never circular imports):
   core → messages → live_view → nuclear → reporting → workers → updates
   → commands → hooks → facade.

## Result

- gateway.py 4,139 → 534 lines, **3 own definitions**
  (process_update_sync, runtime_async, main — the composition root).
  The rest is docstring + re-exports for the suite. (The original <300
  goal was a proxy for "no logic": the logic is 0; the re-exports the
  tests still consume via `zt.X` are the remaining surface, migrable in
  the future.)
- Modules: core 465, messages 215, live_view 525, nuclear 383,
  reporting 813, workers 715, updates 251, hooks 422, commands/
  (registry 53 + dispatch ~800).
- LiveTurnView: the 22 nonlocals of _live_view → attributes; the
  `nonlocal cot_full` bug (14:28 incident) disappears by construction;
  a new battery of 10 tests.
- Suite 311 tests (288 at the start of F4), including the prompt_worker
  happy path — the bot's heart, until then with NO test of the complete
  flow.
- Latent bugs found while moving: `_log_rotate` without an import
  (housekeeping), `ctx_tasks(20)` without an underscore in
  `_resolve_ws` (NameError on the first `/run <tag>`).

## Handler split (declared debt)

The ~25 `_cmd_*` lived interleaved with the registration table in
`commands/dispatch.py` (pure movement; splitting them by domain would
have required reordering bodies). Splitting into handlers_info/turnos/
sistema was declared a later mechanical refactor over a stable module,
with the suite as guard — since done: today commands/ carries
handlers_info.py, handlers_tasks.py, handlers_system.py (plus
handlers_diff.py).
