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
- DONE (P4.4): `live_view.py` CoT attrs renamed (`cot_cajas`→`cot_boxes`,
  `cot_rotulos`→`cot_labels`, `cot_razonada`→`cot_round`,
  `_cot_run_cerrar`→`_cot_run_close`).
- DONE (P4.4): `partes_iguales`→`equal_parts` (utils.formatting).

## P4.2 English migration — workers/core/nuclear/auditor/updates (21/10)

- DONE (P4.4): poller.py's Spanish identifiers renamed
  (`auditar`→`audit_fn`, `_auditar`→`_audit`, `_cola_check`→`_queue_check`,
  `ciclos`→`cycles`, `pollando`→`polling`); gateway/tests updated.
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
- DONE (P4.4): `updates.BUSY_FRESCO_S` renamed to `BUSY_FRESH_S`.
- DONE (P4.4): config.py constant IDENTIFIERS renamed
  (`ARCH_*`→`WORKERS_FILE/QUEUE_FILE/SESSION_FILE/AUDIT_FILE/
  LIVE_SKIP_FILE/RELOAD_FILE/LAST_REPLY_FILE/LAST_TRACE_FILE`,
  `TURNO_LARGO_AVISO_S`→`LONG_TURN_NOTICE_S`, `codigo_cambio`→
  `code_changed`, `CODE_STAMP_INICIO`→`CODE_STAMP_START`); the config
  KEY strings (`rotar_umbral_mb`, `rollout_retain_dias`, `auditor_cada_s`,
  `auditor_alerta_cooldown_min`, `auditor_fallos_min`...) and the
  `bot-auditoria.json` VALUE stay for P4.5/P6.
- DONE (P4.4): `live_view._turn_end_poll`'s `aviso=` kwarg renamed to
  `notice=` (workers + tests updated). messages.py's `solo_ultima=`
  kwarg keeps its name (messages.py is P4.3 territory and kept it).
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
- DONE (P4.4): gateway now passes `audit_fn=zc_auditor.run_audit`
  (poller's kwarg renamed).
- DONE (P4.4): `max_partes`→`max_parts` (reporting signature + call
  sites + test lambdas).
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
- DONE (P4.4): live_view/reporting/hooks/utils.formatting/state/store +
  registry identifier renames applied (see the P4.4 section below);
  zcode_proto.py (`viejo`) and scripts/spike_f7_latency.py remain (no
  phase's file list).
- DONE (P4.4): support.py's `patch(..., nuevo=...)`→`new`, FakeClock
  `paso`→`step`, wait_until `paso`→`step`.
- test_f21_orphans/test_f50/test_process_update/test_f40 fixtures keep
  writing the PERSISTED cache keys (`{"titulo": …, "cajas": …,
  "rotulos": …}`) — on-disk shapes, not identifiers.
- test_f8_closures' Spanish (`trozos`, `que…`) lives inside code-snippet
  STRING fixtures (its own subject); test_higiene's `nombre` loop var
  and test_dispatcher/test_register_outbox/test_f9_eventos Spanish
  words are all string data or untouched files (not hit by P4.3
  greps).

## P4.4 English migration — reporting/live_view/hooks/poller/dispatcher/config/utils/state/telegram (21/10)

- In-memory dict keys kept byte-identical (string literals, per the
  identifier-only rule): live_view's `tools_live` entries still use
  keys `"nombre"/"texto"/"fin"/"salida"` (internal to live_view.py, no
  other reader); dispatcher.submit's reason value `"duplicado"`; the
  webhook JSON response key `"motivo"` (webhook_server + test_webhook
  asserts). Rename together with their owners in a string pass.
- `webhook_server`/`utils/http`/`store.py`/`tasks_repo` Spanish LOG and
  SQL strings stay byte-identical (`"sin secret válido"`, `"sendMessage
  caído, va al outbox"`, `AS es_nueva`, `"lock retenido..."`) — log/
  SQL literals, not identifiers.
- `limites.py` constants still carry Spanish stems (`CH_TURNO`,
  `PARTES_MAX`, `CH_RESPUESTA`, `CH_CAJA`) — the file was in no phase's
  list; its identifiers are referenced by hooks/reporting/messages/
  utils.http. Rename in a limites-focused pass.
- `src/zcode_proto.py` keeps `viejo` (and other Spanish locals) — the
  vendored protocol client was in no phase's list.
- `scripts/check_no_duplicates.py` Spanish internals stand (already
  noted above); only its `ARCH_RELOAD` comment references were updated.
  `scripts/spike_*.py` keep their Spanish locals (spikes, not product).
- `audit.registrar`/`leer` remain audit.py's current (Spanish) public
  names — audit.py was P4.2 territory and kept them; utils/http's call
  was left matching.
- `razonamiento.save_cache(..., vivo=)` / cache key `"vivo"` and the
  `--cot-vivo` flag/glob: immutable-ish contract, still Spanish by
  design (same policy as P4.2's note).
- test fixture strings kept Spanish on purpose (data, not identifiers):
  `"respuesta final del turno"`, `"/x/nuevo.py"`, `"nuevo.txt"`,
  `"-viejo/+nuevo"` diff bodies, `"queda dormido"`, `"¿Seguro?"`,
  `"quota agotada"`, `"opción 2"`; test file/module names with Spanish
  stems (`test_timeouts_sesion.py`, `handlers_tareas`, `test_f27_incidente_red.py`...)
  also stay — file renames were out of scope.
- tests/test_callbacks.py:59 SyntaxWarning is pre-existing (noted above
  under smells); untouched by this pass.
