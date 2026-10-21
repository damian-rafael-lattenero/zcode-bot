# Tech debt & improvement backlog

Findings collected during the F53 English migration (and after) — to be
applied once the translation phases close. Anyone (human or agent) working
in this repo: when you spot something worth improving, append a line here.

Format: one bullet per finding — `file:line-ish` · what · why it matters ·
proposed action. Mark items DONE when fixed.

## Code smells / latent bugs seen

- `src/zcode_bot/commands/handlers_tareas.py` `_cmd_retry` — the notice
  "⏳ A turn is already running in «%s»" never interpolates its `%s`
  (pre-existing bug, carried through the Spanish→English translation
  unchanged). Fix: pass the title or drop the placeholder.
- `tests/test_callbacks.py:58` — SyntaxWarning "'int' object is not
  subscriptable; perhaps you missed a comma?" from a dead expression
  (`"hola mundo" * 20[:64] if False else ...`). Dead fixture code that
  predates F53; delete or fix the expression.
- `src/zcode_bot/auditor.py` module docstring says "four invariants" but
  lists five. Update the count (or number them).
- `tests/test_f17_queue.py` — dead code after the `unittest.main()` call
  (never executes). Delete.
- `src/zcode_bot/razonamiento.py` `wait_reply(cfg, "permiso pendiente" / ...)`
  internal hint labels appear unused (pass-through identifiers asserted by
  tests but never rendered). Confirm and remove the hint parameter or the
  labels.
- DONE (P3b): `utils/validation.py` classify() accent bug — stored literals
  "apruébalo"/"núnca" could never match because norm() strips accents from
  user input; now accentless, plus English words added alongside Spanish.

## Comments & docs

- Hundreds of comments reference F-codes (F32, F34b, R0b, F51a...) that
  nobody outside this project can decode. Proposal: rewrite load-bearing
  comments to be self-explanatory and move the history to the git log/ADRs;
  optionally keep a short F-code glossary in docs/ for the ones that stay.
- Some comments narrate the change that introduced them ("D2: the window
  follows the config, not a fixed...") — review-speak frozen in the code.
  Keep the constraint, drop the story (the story lives in git).

## Refactoring candidates

- R13 (pending since the 20-21/10 refactor): consolidate the CoT test
  fixtures — `_FakeC` exists with 6 different signatures across 9 test
  files, plus `_Th`, `_ev`, `CAP/T0/SID` constants. Move to tests/support.py
  file by file.
- `razonamiento._poll_events` / `_vigilar_turno` are long; a split was
  declared "otro día" on 21/10.
- `deploy.sh` "ritual" greps (`grep '^def running_turns' core.py`, def-count
  of gateway) are brittle string checks — could become import-time asserts
  in a tiny `selfcheck.py` module both deploy and CI call.
- `commands/callbacks.py` carries the legacy `task:N` callback vocabulary
  forever ("old buttons still live in the owner's chats"). Give it a
  removal date once the 24h button TTL has flushed every pre-F52 button.
- `check_no_duplicates.py` — Spanish identifiers remain (revisar_archivo,
  dueño, permitidos...); rename when the gate itself is touched (P4/P5
  will pass through it).
- Public functions have zero type annotations (stdlib-only repo, no
  mypy/pyright in CI). 2026 practice would annotate at least the public
  surface + add a CI typecheck step; decide consciously, it is a big diff.
- Cosmetic phrasing (declared 21/10): orphan-notice wording after a
  voluntary /stop; cot-cache `vivo=True` left behind after /stop.

## Tooling / CI

- pytest.ini has no `-p no:cacheprovider` and tests re-insert `sys.path`
  boilerplate in ~35 files — a single conftest.py could replace it (house
  rule forbade conftest for determinism; revisit with a deliberate test).
- CI runs the suite twice locally (-n 16 and -n auto) by habit; the GitHub
  runner only does -n auto. Consider recording WHY two local runs matter
  (xdist ordering flakiness) in the runbook, or drop one.
