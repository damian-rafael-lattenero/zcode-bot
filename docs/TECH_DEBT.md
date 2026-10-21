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

## P4.1 English migration — razonamiento (21/10)

- `razonamiento.py` module NAME itself is still Spanish (importers alias
  it as rz/cotrz/_rz); renaming the module is a later phase — the
  persisted flag `--cot-vivo` and the pid glob `cot-vivo-` already
  survived this pass untouched.
- Dict keys of the cot cache stay Spanish (`cajas`, `rotulos`,
  `titulo`, `vivo`, `finalizado`, legacy `bloques`) — old caches on disk
  must keep opening; renaming them is its own migration phase.
- `watcher_alive` still lazy-imports `workers._proc_vivo` (Spanish name
  in workers, not renamed here — workers is another importer's file).
  UPDATE (P4.2): workers renamed it to `_proc_alive`; the lazy import now
  uses the English name.
- `live_view.py` keeps its own Spanish CoT attrs (`cot_cajas`,
  `cot_rotulos`, `cot_razonada`, `_cot_run_cerrar`) — this pass only
  owned the rz.* call lines.
- `partes_iguales` (utils.formatting) is Spanish and imported by
  razonamiento; belongs to a utils-focused migration pass.

## P4.2 English migration — workers/core/nuclear/auditor/updates (21/10)

- `poller.py` keeps its OWN Spanish identifiers (out of P4.2's five
  files; only call lines/comments were touched): the `auditar=None`
  param + `self._auditar`, `self._cola_check`, `self.ciclos`,
  `pollando`. gateway now passes `auditar=zc_auditor.run_audit` and
  `queue_drain=queue_check` — the Spanish kwarg names survive because
  they are poller's signature.
- Persisted dict KEYS stay Spanish while their dataclass FIELDS are now
  English — a P6-style dual-marker pass would be needed to migrate the
  on-disk shapes: `WorkerEntry.title` writes key `"titulo"`
  (state/bot-workers.json), `NuclearReport.stopped/total/queued/alive`
  write keys `"frenadas"/"totales"/"cola"/"vivos"`
  (state/bot-nuclear-report.json), `_active_turn_register` writes key
  `"titulo"` (bot-active.json), audit.py writes `"tipo"/"detalle"`
  (registrar args renamed to kind/detail).
- `_cot_vivo_ok` (workers) and the `es_cot_vivo` param of
  `_nuc_kill_own_runtimes` keep the "vivo" stem on purpose: they verify
  the IMMUTABLE `--cot-vivo` CLI flag / `cot-vivo-*.pid` glob, so the
  name mirrors what it matches (same policy as BUSY_FRESCO_S below).
- `updates.BUSY_FRESCO_S` kept per the P4.2 naming map ("fresco" in a
  module-level constant; rename when updates.py is next touched
  deliberately).
- `config.py`'s own Spanish names are untouched (P5/P6 territory):
  `ARCH_COLA`/`ARCH_SESION`/`ARCH_WORKERS`/`ARCH_AUDITORIA` and the
  config keys `rotar_umbral_mb`, `rollout_retain_dias`,
  `auditor_cada_s`, `auditor_alerta_cooldown_min`, `auditor_fallos_min`,
  plus `config.codigo_cambio()`.
- `live_view._turn_end_poll`'s `aviso=` kwarg is live_view's own Spanish
  param (live_view not in this phase); workers passes
  `aviso=long_turn_notice` through it. Same for messages.py's
  `solo_ultima=` kwarg kept at workers' call sites.
- `audit.registrar`/`leer` function names stay (fine English-ish); only
  the args were renamed (tipo→kind, detalle→detail) — the one kwarg call
  site (auditor._alert) was updated; every other call is positional.
- tests/test_f17_queue.py carries DEAD CODE after `unittest.main()`: two
  async test defs (full-queue notices) inside the `__main__` block,
  never collected by pytest — move them into a class or delete.
- tests/test_prompt_worker.py still holds Spanish fixtures/locals
  (`_ViewRapida`, `entregas`, `metodos`, `esperado`, `tit`/`texto` in
  lambdas) — the file references no P4.2 rename, so it was left for its
  own pass.
- tests/test_f6_nuclear.py and test_auditor.py still assert on the
  PERSISTED Spanish dict keys (`rep["frenadas"]`, `e["tipo"]`,
  `{"titulo": ...}`) — correct: those are on-disk shapes, not
  identifiers.

## P4.3 English migration — commands/messages/espectador/avisos/gateway (21/10)

- The callbacks' internal STYLE TAG VALUES stay Spanish strings
  (`"ar"`/`"esp"`/`"ses"`/`"libre"` in commands/callbacks.py) — they are
  in-memory discriminators, byte-identical per the identifier-only rule;
  the constants are now `_AR`/`_ESP`/`_SES`/`_FREE`. Rename the values
  only if callbacks.py is ever rewritten with persisted styles.
- `handlers_tareas._cmd_retry` still reads the workers-store key
  `"nueva"` (`_workers().get(... or "nueva")`) and `_cmd_stop` passes
  `w.get("titulo")` — both are state/registry.py's PERSISTED shapes
  (see the P4.2 note: key "titulo" migration is P6 territory).
- `gateway.runtime_async` passes `auditar=zc_auditor.run_audit` to
  `poller.supervisor_loop` — the Spanish kwarg is poller.py's signature
  (poller's own names are another batch, same as P4.2's `queue_drain=`
  note).
- `reporting._send_long_reply(..., max_partes=10)` keeps the Spanish
  kwarg at handlers_info's call site: the param belongs to reporting.py
  (P4.4+).
- `razonamiento` cache dict keys read by handlers_info (`"rotulos"`,
  `"ph"`, `"phs"`) stay — persisted on-disk shapes, foreign module.
- `messages._tool_box_on` still gates on config key `"tools_caja_filtro"`
  with values `"todas"/"accion"/"minimo"` — a persisted CONFIG key
  (config.py territory, P5/P6).
- `spectador`'s pwlog/log strings stay byte-identical Spanish
  (`"espectador: turno fijado …"`, `"espectador fin: pasos=%d"`,
  `"queue_mutate FALLO: %s"` in handlers_tareas) — string literals were
  out of scope (identifier-only pass); the `"--espectador"` CLI flag and
  the `espectador`/`avisos` MODULE names also stay.
- live_view.py keeps its OWN Spanish locals/dict keys (`nombre`,
  `sal`, `cuerpo`, `trozos`, `tools_vivo`, `e["salida"]`, `titulo`
  attr...) — only the renamed messages/ imports and call lines were
  touched here; live_view is a later batch. Same for reporting.py
  (`marca`, `activos`, `titulo`, `partes` locals), hooks.py (`partes`,
  `trozos`), utils/formatting.py (`partes_iguales`, `sello_lineas`,
  `lineas`), state/store.py (`nuevo`), state/registry.py (`titulo`
  keys), zcode_proto.py (`viejo`) and scripts/spike_f7_latency.py
  (`paso(nombre, fn)`).
- tests/support.py's `patch(..., nuevo=...)` kwarg is Spanish — support
  is shared harness owned by no single phase; left for a support.py
  pass.
- test_f21_orphans/test_f50/test_process_update/test_f40 fixtures keep
  writing the PERSISTED cache keys (`{"titulo": …, "cajas": …,
  "rotulos": …}`) — on-disk shapes, not identifiers.
- test_f8_closures' Spanish (`trozos`, `que…`) lives inside code-snippet
  STRING fixtures (its own subject); test_higiene's `nombre` loop var
  and test_dispatcher/test_register_outbox/test_f9_eventos Spanish
  words are all string data or untouched files (not hit by P4.3
  greps).
