# Tech debt & improvement backlog

Findings collected during the F53 English migration (and after). The
collected backlog was APPLIED on 21/10 in waves W1–W5 (commits 21b70bf,
ffb0a22, c93636d, d858791, a794a47, 7f9bfee + the W4/W5 ones) — everything
actionable is marked DONE below. Still open by design: the
`_poll_events`/`_watch_turn` split (Damian deferred it), type
annotations (his call — big diff), and the `task:N` callback removal
(dated 22/10, needs his go). Anyone (human or agent) working in this
repo: when you spot something worth improving, append a line here.

Format: one bullet per finding — `file:line-ish` · what · why it matters ·
proposed action. Mark items DONE when fixed.

## Code smells / latent bugs seen

- DONE (tech-debt W1): `handlers_tasks._cmd_retry` — the busy notice now
  interpolates the running turn's title (bilingual read via
  `registry.entry_title`); regression test `TestRetry` in
  test_f17_queue.py asserts `«title»` renders and no bare `%s` survives.
- DONE (tech-debt W1): test_callbacks.py dead expression
  (`"hello world" * 20[:64] if False else ...`) deleted — the
  SyntaxWarning at import is gone.
- DONE (tech-debt W1): `auditor.py` docstring says "five invariants"
  (it lists five since F51a).
- DONE (P4.6, confirmed): test_f17_queue.py dead code after
  `unittest.main()` — the two `test_full_queue_*` methods were moved
  into `TestQgoQdel` (suite 647→649 at the time); F51b is covered live.
- DONE (tech-debt W1): `wait_reply(cfg, hint)` — the hint parameter was
  dead; it now labels the getUpdates failure log ("wait, pending
  approval") instead of being removed (15+ test fakes take `(cfg, hint)`;
  making it load-bearing is the smaller, useful diff).
- DONE (P3b): `utils/validation.py` classify() accent bug — stored literals
  "apruébalo"/"núnca" could never match because norm() strips accents from
  user input; now accentless, plus English words added alongside Spanish.
- DONE (P6): the on-disk Spanish shapes migrated with permanent bilingual
  readers — see the P6 section at the bottom for the full map and the
  survivors that are permanent by design.
- `tests/test_f39_stop.py:6,54,82` and `tests/test_f21_orphans.py:268` —
  deliberate Spanish survivors of P4.6: negative asserts/docstring quotes
  that lock REMOVED Spanish banners («... el reporte llega ...»,
  'frenado por /stop', "frenada"). A regression would restore the Spanish
  text, so the guards must keep the exact old wording; revisit at P6 (mark
  `# noqa: spanish` or reword once the formats migrate).
  UPDATE (P6): the quoted banners stayed quotes — the formats they name
  (`titulo`/`vivo` keys, `--cot-vivo` flag) migrated with PERMANENT
  bilingual readers, so the guards still describe real history. Left
  as-is.
  UPDATE (P8): marked `# noqa: spanish` — the P8 gate would otherwise
  flag them (quoted history is a permanent exception by policy).

## Comments & docs

- DONE (tech-debt W2, ffb0a22): docs/F-CODES.md — glossary decoding all
  ~90 provenance tags (F features, R refactors, D discrepancies), plus a
  README house rule ("prose next to a tag must stand on its own").
  Survey verdict: virtually every tagged comment already carries a
  self-contained explanation (`# F42: <explanation>` is the convention),
  so the 483 in-code tags STAY — no mass rewrite. New tags must add a
  glossary line in the same commit.
- Kept deliberately (W2 decision): short contrastive clauses
  ("FULL prompt (before: 200-char preview)") are behavior documentation,
  not review-speak — they stay.

## Refactoring candidates

- DONE (tech-debt W3b, 7f9bfee): R13 — the events-stream fake and the
  thread fake consolidated into `support.FakeEventsClient` /
  `support.FakeThread` (live_view, f45, f42, f44, f15 migrated;
  -142/+69). The SESSION fakes (prompt_worker's session/messages
  replay, rotation, timeouts, f21's client) stay local BY DESIGN: they
  simulate a different protocol surface, sharing only the name. The
  `_ev`/`CAP`/`T0` per-file constants stay local too (values differ
  per file; moving them buys nothing).
- `reasoning._poll_events` / `_vigilar_turno` are long; a split was
  declared "otro día" on 21/10.
- DONE (tech-debt W3c, d858791): `deploy.sh` ritual greps →
  `scripts/selfcheck.py` (AST-based: running_turns ×1 in core, gateway a
  pure facade of exactly process_update_sync/runtime_async/main), called
  by deploy.sh AND ci.yml. Fail-then-pass proven (a 4th gateway def is
  caught with a named message).
- Cosmetic phrasing (declared 21/10): orphan-notice wording after a
  voluntary /stop; cot-cache `vivo=True` left behind after /stop.
  UPDATE (tech-debt W3d): the two /stop orphan notices reworded («it
  did not answer the cooperative stop in time and was killed» /
  «the runtime was killed before it could close the turn»). The
  `vivo=True` item: evidence review found every main path COVERED and
  tested (cooperative stop closes via the live sidecar's sealer; dead
  worker + stop flag → _orphans_check closes with error "stopped by
  /stop", test_cot_cache_after_stop_says_stopped; any residual cache is
  healed by auditor._orphan_caches). The only cosmetic residue: the
  auditor closes a no-entry cache with the generic reason "watcher
  died" — once bot-active is cleared it cannot know it was a voluntary
  /stop. Left as-is (indistinguishable by design).
- `commands/callbacks.py` carries the legacy `task:N` callback vocabulary
  forever ("old buttons still live in the owner's chats").
  UPDATE (tech-debt W4, 21/10): the 24 h TTL of the last pre-F52 button
  expires at the end of 21/10 — REMOVABLE FROM 22/10. Removal still
  needs Damian's explicit go: compat surfaces in this repo are removed
  deliberately, never opportunistically (same policy as the bilingual
  classify() words, which stay forever by his decision).
- DONE (P8): `check_no_duplicates.py` — Spanish identifiers renamed
  (revisar_archivo→check_file, dueño→owner, archivos→files_list,
  propios→own_names, attrs_ausentes→missing_attrs, mod_nombres→
  module_names, permitidos→allowed, nombres→names, definiciones→
  definitions, errores→errors, modulo→module_name, MODULOS_CONST→
  CONST_MODULES); behavior unchanged (gate still passes at 0). The
  `"shim de compatibilidad"` literal in is_declared_shim stays forever
  (permanent dual marker) with `# noqa: spanish`.
- Public functions have zero type annotations (stdlib-only repo, no
  mypy/pyright in CI). 2026 practice would annotate at least the public
  surface + add a CI typecheck step; decide consciously, it is a big diff.

## Tooling / CI

- DONE (tech-debt W3a, c93636d): tests/conftest.py now owns the sys.path
  setup; the per-file boilerplate was stripped from 30 files. The old
  determinism worry is covered: suite green from repo root AND tests/,
  -n 16 and -n auto, plus single-file runs.
- CI runs the suite twice locally (-n 16 and -n auto) by habit; the GitHub
  runner only does -n auto. Consider recording WHY two local runs matter
  (xdist ordering flakiness) in the runbook, or drop one.

## P4.1 English migration — razonamiento (21/10)

- DONE (P5): `razonamiento.py` module NAME was renamed to `reasoning.py`
  (importers re-pointed; the persisted flag `--cot-vivo` and the pid
  glob `cot-vivo-` stay frozen).
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
  UPDATE (tech-debt W5): the names themselves were renamed too —
  `registrar`→`record`, `leer`→`read` (every caller updated).
- tests/test_f17_queue.py carries DEAD CODE after `unittest.main()`: two
  async test defs (full-queue notices) inside the `__main__` block,
  never collected by pytest — move them into a class or delete.
- DONE (tech-debt W4): test_prompt_worker's Spanish locals renamed
  (`_ViewRapida`→`_QuickView` — also the imports in test_workers_warm
  and test_rotacion —, `metodos`→`methods`, `esperado`→`expected`).
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
- `handlers_tasks._cmd_retry` still reads the workers-store key
  `"nueva"` (`_workers().get(... or "nueva")`) and `_cmd_stop` passes
  `w.get("titulo")` — both are state/registry.py's PERSISTED shapes
  (see the P4.2 note: key "titulo" migration is P6 territory).
- DONE (P4.4): gateway now passes `audit_fn=zc_auditor.run_audit`
  (poller's kwarg renamed).
- DONE (P4.4): `max_partes`→`max_parts` (reporting signature + call
  sites + test lambdas).
- `reasoning` cache dict keys read by handlers_info (`"rotulos"`,
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
  UPDATE (P8): the live_view `tools_live` keys WERE renamed
  (`nombre→name`, `texto→text`, `fin→mark`, `salida→output` — no
  external reader, verified); `"duplicado"`/`"motivo"` were already
  migrated by P6.
- `webhook_server`/`utils/http`/`store.py`/`tasks_repo` Spanish LOG and
  SQL strings stay byte-identical (`"sin secret válido"`, `"sendMessage
  caído, va al outbox"`, `AS es_nueva`, `"lock retenido..."`) — log/
  SQL literals, not identifiers.
  UPDATE (P8): store.py's flock-timeout message translated ("lock held
  by another thread/process"); telegram/client.py's RuntimeError too
  ("Telegram token not configured"). The remaining quoted strings above
  live in files/contexts not flagged by the P8 detector.
- DONE (P5): `limites.py`→`limits.py` and its constants renamed
  (`CH_TURNO`→`CH_TURN`, `CH_RESPUESTA`→`CH_REPLY`, `CH_CAJA`→
  `CH_BOX`; `PARTES_MAX` stem stays) — hooks/reporting/messages/
  utils.http/tests updated.
- DONE (P5): `src/zcode_proto.py`'s `viejo` (and `cerrados`/`pend`)
  renamed to English locals.
- `scripts/check_no_duplicates.py` Spanish internals stand (already
  noted above); only its `ARCH_RELOAD` comment references were updated.
  `scripts/spike_*.py` keep their Spanish locals (spikes, not product).
- `audit.registrar`/`leer` remain audit.py's current (Spanish) public
  names — audit.py was P4.2 territory and kept them; utils/http's call
  was left matching.
  UPDATE (tech-debt W5): no longer — renamed to `record`/`read`
  (supersedes this note and the P4.2 one).
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

## P5 English migration — module renames (21/10)

- DONE (P5): modules renamed with git mv (history preserved):
  `razonamiento`→`reasoning`, `espectador`→`spectator`, `avisos`→`acks`,
  `limites`→`limits`, `commands.handlers_tareas`→`handlers_tasks`,
  `commands.handlers_sistema`→`handlers_system`, `test_espectador`→
  `test_spectator`, `test_avisos`→`test_acks`. All imports (incl. the
  lazy ones in hooks/auditor/nuclear/gateway), comments/docstrings,
  `check_no_duplicates` keys (ALLOW_MAGIC `spectator`/
  `commands.handlers_system`, MODULOS_CONST `limits`) updated.
- DONE (P5): `limits.py` constants renamed `CH_TURNO`→`CH_TURN`,
  `CH_RESPUESTA`→`CH_REPLY`, `CH_CAJA`→`CH_BOX` (`PARTES_MAX` stays —
  its own stem); support.py re-exports and every CH_* user updated.
- DONE (P5): `zcode_proto.py` Spanish locals renamed (`cerrados`→
  `flush_complete`, `viejo`→`old`, `pend`→`pending`).
- Import ALIASES still abbreviate the old Spanish names (kept to keep
  the diff mechanical): `_rz` (hooks, nuclear), `zc_rz` (auditor),
  `rz` (12 test files), `esp` (test_spectator), `zc_sist`/
  `zc_tareas`/`zc_sistema`/`ht` (tests). `cotrz`→`cotmod` WAS renamed
  (live_view, workers) and `lim` became plain `limits`. P6 can finish
  the alias cosmetics.
  UPDATE (tech-debt W5): DONE — every one of those aliases now imports
  the plain module name (`reasoning`, `spectator`, `handlers_system`,
  `handlers_tasks`); the hygiene gate's alias list follows the new
  names.
- RESOLVED (P6 string pass): `spectator.py` log/audit strings were
  half-migrated prose (`"spectator: turno fijado …"`,
  `"spectator fin: pasos=%d"`) — they are English now (e.g.
  `"spectator: turn pinned %s"`, `"spectator end: steps=%d"`). NOTE:
  the audit kind `"espectador"→"spectator"` is a PERSISTED `tipo`
  value (bot-audit log) — records before 21/10 keep the Spanish kind;
  any future tooling that filters by kind must accept both.
- Frozen survivors (byte-identical by design): the `--espectador` CLI
  flag (gateway parse, hooks spawn, usage text, test_f15 asserts), the
  `mac_espectador`→`mac_spectator` legacy knob alias in config.py, the
  `--cot-vivo`/`--cot-botones` flags and their pid/glob names, and the
  `es_run_turn` param of `auditor._orphan_caches` (Spanish stem, out
  of P5's identifier list).
  UPDATE (tech-debt W5): `es_run_turn` was renamed to `is_run_turn`
  (the flags/knob/glob survivors above stay frozen).
- `check_no_duplicates.py` internals still Spanish (`revisar_archivo`,
  `dueño`, `MODULOS_CONST`...) — pre-noted above; only its module keys
  were updated by this pass.

## P6 English migration — persisted formats, flags, audit trail (21/10)

DONE (P6): every persisted state format now WRITES English-only and READS
bilingual (old+new) forever — a 24h-old Telegram button or a file written
by a pre-deploy process keeps working across deploys.

- Mappings applied (write → new; read accepts old fallback):
  - CoT cache `cot-<sid>-<t0>.json`: `titulo→title`, `cajas→boxes`,
    `rotulos→labels`, `vivo→live`, `finalizado→done`; legacy `bloques`
    still read as the boxes of pre-F46 caches. Bilingual reads:
    `reasoning.show`, `close_cache`, `cache_closed_or_live`,
    `commands.handlers_info._cmd_cotb`, `auditor._orphan_caches`.
  - `bot-workers.json` / `bot-active.json` / `bot-q/q-*.json`: key
    `titulo→title` (WorkerEntry.to_dict, `_active_turn_register`,
    `QuestionFiles.ask`); bilingual via `state.registry.entry_title`
    (registry.flag_stop, reporting, handlers_tasks._cmd_stop,
    workers._orphans_check, WorkerEntry.from_dict).
  - `bot-nuclear-report.json`: `frenadas→stopped`, `totales→total`,
    `cola→queued`, `vivos→alive` (NuclearReport.to_dict/from_dict).
  - Audit log: file `bot-auditoria.json→bot-audit.json`
    (`config.AUDIT_FILE_LEGACY` keeps the old name for the read fallback
    and the housekeeping purge); entry keys `tipo/detalle/clave→
    type/detail/key`; kind VALUES `turno-muerto→dead-turn`,
    `marker-huerfano→orphan-marker`, `cola-dormida→idle-queue`,
    `cot-huerfano→orphan-cot`, `alerta→alert`, `envio→send`,
    `espectador→spectator`; anti-spam key words `fallos→failures`,
    `cola→idle-queue`, `fantasma→ghost`. `audit.leer()` normalizes old
    entries (and reads the old FILE only while the new one is absent);
    `auditor` correlates on the normalized form.
  - Dispatcher/webhook in-memory: reason `"duplicado"→"duplicate"`,
    webhook JSON key `"motivo"→"reason"` (+ `"ignorado"→"ignored"`).
    The webhook response body is opaque to Telegram (only the 200
    status matters), so no dual key on that surface.
  - CLI flags: `--cot-vivo→--cot-live`, `--espectador→--spectator`,
    `--cot-botones→--cot-buttons` (hooks spawns, gateway parse/usage,
    shim docstring); pid file `cot-vivo-<sid>.pid→cot-live-<sid>.pid`.
- PERMANENT dual checks (never remove):
  - `workers._cot_live_ok` matches BOTH `--cot-live` and `--cot-vivo` in
    the cmdline — pre-deploy watchers must stay killable
    (`tests/test_stop_brusco.py::test_cot_live_ok_matches_old_and_new_flags`).
  - `reasoning.watcher_pids/watchers_cleanup` glob BOTH `cot-live-*.pid`
    and `cot-vivo-*.pid` (`_PID_PREFIXES`).
- Known transitional behavior (by design, ages out):
  - A watcher spawned pre-deploy keeps heartbeating its OLD
    `cot-vivo-<sid>.pid`. First written up as "watcher_alive reads
    only the new pid name → a fresh hook may spawn a second watcher
    for one turn right after the deploy (window: one keepalive,
    ≤10 min)". CORRECTED HISTORY: `pid_read` falls back to the old
    `cot-vivo-` name as well (reasoning.py:512, landed in the same P6
    commit), so there is NO duplicate-spawn window — an old watcher is
    seen as warm and single-flight holds across the deploy.
  - `leer()` stops reading `bot-auditoria.json` the moment
    `bot-audit.json` exists; housekeeping purges both after 2 idle days.
- Compat tests added: `TestLegacyAuditCompat` (test_auditor.py — old
  file+keys+kinds normalize, correlate and suppress), 
  `TestLegacyCacheCompat` (test_f40_cot_botones.py — old cache shows,
  closes and feeds ct: buttons), 
  `test_cot_live_ok_matches_old_and_new_flags` (test_stop_brusco.py),
  `test_report_persists_new_keys_and_reads_both` (test_f6_nuclear.py;
  `_watchdog_env` keeps the legacy shape as a live fixture).
- Survivors (kept on purpose):
  - Comments/docstrings quoting Damian's verbatim Spanish remarks or
    incident banners (config.py «se tildó», live_view «acumularon
    todas», messages «lo vi todo junto», workers «no frenó en 4s» /
    «falló», core.py "reiniciá ZCode", formatting.py's quote) — they are
    quotes, not code strings.
- `utils/validation.py` `[¡¿]` in the punctuation regex — genuine
  user-input data handling.

## P7 English migration — docs (21/10)

- DONE (P7, fixed in the translated runbook): docs/runbook.md said the
  /nuclear cycle log marker in prompt-worker.log was `"NUCLEAR"`, but
  `src/zcode_bot/nuclear.py` (Nuclear 2.0) logs `"NUCLEAR2 start/stop/
  kickstart/watchdog spawned/verified/FAILED"` — grepping the documented
  marker during an incident would find nothing. Runbook now quotes
  "NUCLEAR2". Operationally relevant drift; any other doc quoting log
  markers should be re-checked when touched.
- DONE (P7): README.md rewritten to English against the final
  post-migration names (modules, knobs, CLI flags, state files, ADR
  slugs). Factual drift found between the OLD README and the current
  code while verifying (the rewrite documents the current truth; the
  code-side items below remain actionable):
- README knob table vs config.DEFAULTS: `wait_timeout_s` was documented
  as 240 and `question_wait_timeout_s` as 60 — both are 86400 since
  F52 (phone buttons wait 24 h). Anyone sizing the permission/question
  windows off the old README was wrong by 144x/360x.
- Old README knob table labeled `warm_keepalive_s` (600) in "minutes" —
  the value is SECONDS (10 min). Mislabel carried since F8; fixed in
  the README only.
- Test counts: old README said 319 tests / suite ~6 s (e2e section:
  342). Current: 659 passed + 11 subtests in 57 files, 44 s with
  `-n auto` (measured 21/10). The ~6 s claim had drifted badly.
- "docs/adr/ 13 ADRs" → 18 (slugs now English).
- F30 section documented FOUR auditor invariants; auditor.py has FIVE
  (the 5th: orphan CoT caches, F51a) — same mismatch as the docstring
  bullet above. README now lists five; `auditor.py`'s docstring still
  says "four" (that bullet stays open).
- F7 section described the CoT as permanent «CoT N» text messages —
  superseded by F42/F43 (`cot_buttons` default True: size-sealed boxes
  as growing buttons; the «CoT N» text path only renders when
  `cot_buttons=False`, see live_view._cot_flush). README updated;
  nothing to fix in code.
- Old F9 section named handlers_tareas/handlers_info/handlers_sistema
  and `support.parchea` — now handlers_tasks/handlers_info/
  handlers_system (+ handlers_diff, R8) and `support.patch`.
- Old F31 cited `core.ultimo_uso(sid)` — renamed to `core.last_used`
  during the migration.
- Architecture diagram said "~25 handlers" — commands/dispatch.py now
  registers 31 CommandSpecs.
- F32's header gained the 🖥️ Mac origin label (19/10, hooks._turn_data)
  after the README section was written; now documented.

## P8 English migration — final zero + enforcement gate (21/10)

DONE (P8): every `.py` under src/, tests/ and scripts/ is at ZERO
Spanish (per `scripts/check_english.py`), enforced by a fifth hygiene
gate (`TestOnlyEnglish` in tests/test_higiene.py) that calls the SHARED
detector (`check_english.spanish_lines`) — metric and gate cannot drift.

Renames of this pass:
- `reasoning.run_vigilante`→`run_watcher` (callers: reasoning.cmd_cot_live,
  test_f41/test_f44/test_f45) and every prose/pwlog "vigilante"→"watcher"
  (pwlog: "another watcher took the session — stepping down"). The word
  survives only inside check_english's stopword list, where it belongs.
- `check_no_duplicates.py` internals (see the DONE note above).
- locals: `con`→`conn` (tasks_repo._db, formatting.task_label — "con" is
  a Spanish stopword), `lo`→`low` (formatting.html_clip binary search).
- live_view `tools_live` dict keys (see the P4.4 UPDATE note above).
- spike_f7a's `hola-f7` marker → `hello-f7`; messages.py docstring now
  documents `tools_box_filter` (was quoting the pre-P6 key/values).

Detector change (deliberate): "del" was REMOVED from check_english's
STOPWORDS — it is a Python keyword and flagged every `del x[k]`
statement in the tree (dispatcher, live_view, formatting, zcode_proto),
a systematic false positive; per the script's own rule, a word that is
also part of the code's language must not be a stopword. Spanish "del"
in prose is still caught by its neighbors (de/el/la/accents).

PERMANENT `# noqa: spanish` exceptions (79 lines total; every one
carries a short reason). Rule: noqa = load-bearing forever — compat
data or quoted history; prose always translates.
- Bilingual USER INPUT (~10): validation.py's YES/NO word sets + the
  `[¡¿]` punctuation regex; test_pure's `cl("si")`/`cl("SÍ, dale")`;
  test_f15/test_f26 `("yes", "si")` classify returns.
- Legacy callback VALUE (~5): callbacks.py `"si": "si"` +
  `is_question_reply`'s `("yes", "si", "no")`; test_callbacks ×2;
  test_f50's `{"value": "si"}` old button.
- Persisted-shape COMPAT (~35): audit LEGACY_KINDS/LEGACY_KEY_WORDS;
  config `_TOOL_FILTER_VALUES` (+ LEGACY_KNOBS keys, unflagged);
  registry/workers/handlers_info/reasoning bilingual `.get("titulo")/
  "cajas"/"rotulos"` reads and the docstrings documenting them; nuclear
  from_dict's `("frenadas"/"totales"/"cola"/"vivos")`; dispatch's /cola
  legacy alias; test_f40's old-cache fixture ×8, test_f6's legacy
  report shape ×2, test_auditor's legacy audit fixture ×3,
  test_concurrency/test_stop_brusco single lines.
- Quoted HISTORY (~20): Damian's verbatim remarks and old incident
  banners — config «se tildó», core "reiniciá ZCode" + 'apruebo y me
  lo pide otra vez', workers «no frenó en 4s»/«frenado por /stop»/
  «falló» ×2, live_view «CoT en botones»/«sin CoT»/«en la última box
  se acumularon todas», formatting's F50b quote ×4, messages «lo vi
  todo junto», callbacks' «Agregar algo al plan» hook label,
  test_f39's negative asserts locking the REMOVED Spanish banners ×3,
  test_auditor's docstring quote.
- The gate's OWN docstring examples (test_higiene.py ×7) — quoting the
  exceptions it polices.
- check_no_duplicates' `"shim de compatibilidad"` dual marker ×2.

Findings of the pass (new debt):
- DONE (tech-debt W4): `scripts/check_english.py --check` now gates
  .py files ONLY (same surface as the TestOnlyEnglish gate, exit 0
  today); the docs quotes (TECH_DEBT, ADRs, README, REFERENCES) stay
  REPORT-only by policy — quoted history in prose, never a red build.
- DONE (tech-debt W1): test_higiene's detector import is aliased
  (`import check_english as _check_english`).

## tech-debt W5 — last cosmetic Spanish leftovers (21/10)

DONE (W5), all mechanical, suite green (661 ×2: -n 16 and -n auto):
- Import aliases with Spanish stems dropped for the plain module
  names, grep-zero per old alias: `_rz` (hooks.py ×2, nuclear.py) and
  `zc_rz` (auditor.py) → `reasoning`; test aliases `rz` (10 files:
  f40/f41/f42/f44/f45/f46/f21_orphans/f50_auditoria/test_auditor/
  test_process_update) → `reasoning`; `esp` (test_spectator) →
  `spectator`; `zc_sist`/`zc_sistema` (test_cmds_deploy, test_f6)
  → `handlers_system`; `zc_tareas` (test_f17_queue, test_f16) and
  `ht` (test_f50_auditoria) → `handlers_tasks`. The rebind gate's
  alias list (test_higiene `REBIND_PLANO`) now watches the new names.
- `auditor`'s `es_run_turn` param → `is_run_turn` (run_audit + every
  check function + the test call sites; supersedes the P5 frozen-
  survivor note).
- `audit.registrar`/`leer` → `record`/`read` — src callers (auditor,
  workers, gateway, spectator, utils/http) and tests updated; behavior
  identical (the persisted-shape normalization — LEGACY_KINDS,
  tipo/clave reads — is untouched). Supersedes the P4.2/P4.4 "they
  stay" notes.
Still open by design (unchanged): the `_poll_events`/`_watch_turn`
split (Damian deferred it), public type annotations (his call — big
diff), and the `task:N` callback removal (dated 22/10, pending
Damian's explicit go).

## F54b — on-disk schemas (21/10)

- DONE: src/zcode_bot/schemas.py — one TypedDict per persisted format
  (CotCache+Extra, QueueItemShape, WorkersEntry, ActiveTurn,
  QuestionFile/AnswerFile, AuditEntry, NuclearReportShape, SessionShape),
  each docstring declaring the legacy Spanish fallbacks the bilingual
  readers keep accepting (detector-allowlisted by design: the module
  NAMES those keys). tests/test_schemas.py pins every required/optional
  key set and checks the entity to_dict() outputs against them — key
  drift now fails the suite. QueueItem.act honestly Optional; audit.read
  -> List[AuditEntry]. Suite 664.

## F54c — public type annotations (21/10)

- DONE: signatures (params + returns only, zero behavior change) of the
  public surface of core, workers, reasoning, live_view, state/registry,
  audit, config — typing-module style (Dict/List/Tuple/Optional) to
  match schemas.py. Entities pinned to the F54b shapes:
  QueueItem.to_dict -> QueueItemShape, WorkerEntry.to_dict ->
  WorkersEntry, registry get/pop -> Optional[WorkersEntry], items() ->
  (str, WorkersEntry)[], QuestionFiles.pendings/first_pending ->
  QuestionFile. cfg params are plain Dict[str, Any] (no invented Config
  type). Suite 664 green, check_no_duplicates green.
- Surfaced by the pass (mypy checks a body only once its def is
  annotated; all are inference limitations, NOT logic bugs — fixing
  them needs LOCAL/ATTRIBUTE annotations, out of F54c's
  signatures-only scope): config.py:191 (DEFAULTS is a mixed-type
  dict[str, object] → .get overload), core.py:103 (to_dict's `d`
  literal infers dict[str, str] before the conditional model/mode
  keys), registry.py:156/294/311 (store reads are dict[Any, Any] vs
  the WorkersEntry/QuestionFile contracts; `out = []` + early return),
  live_view.py:430/433/435/446/447/451 (`self.seen`/`tools_live`/
  `steps`/`cot_boxes`/`cot_labels`/`cot_phs` need attribute
  annotations), workers.py:1120 (`stop_done = []`). Proposed next
  step (F54d?): one annotation per line above — pure local/attribute
  types, no rewrites.

## F54c — typecheck (21/10, update)

- DONE: mypy 2.4.0 (dev-only, uv venv, pinned), mypy.ini (report-only
  posture), CI step with continue-on-error. 94 public signatures
  annotated across core/workers/reasoning/live_view/state.registry/
  audit/config (schemas TypedDicts wired into to_dict/read/registry
  returns), plus the surfaced one-line annotation fixes (DEFAULTS,
  entity caches, LiveTurnView attrs, INFLIGHT, _SPAWNED...).
  **Baseline: mypy reports 0 errors across 53 files** — the CI step is
  clean from day one; any future error is signal, not noise. Blocking
  can be flipped when that habit holds.
