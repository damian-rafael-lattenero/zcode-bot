# Tech debt & improvement backlog

Findings collected during the F53 English migration (and after) — to be
applied once the translation phases close. Anyone (human or agent) working
in this repo: when you spot something worth improving, append a line here.

Format: one bullet per finding — `file:line-ish` · what · why it matters ·
proposed action. Mark items DONE when fixed.

## Code smells / latent bugs seen

- `src/zcode_bot/commands/handlers_tasks.py` `_cmd_retry` — the notice
  "⏳ A turn is already running in «%s»" never interpolates its `%s`
  (pre-existing bug, carried through the Spanish→English translation
  unchanged). Fix: pass the title or drop the placeholder.
- `tests/test_callbacks.py:59` — SyntaxWarning "'int' object is not
  subscriptable; perhaps you missed a comma?" from a dead expression
  (`"hello world" * 20[:64] if False else ...`; string translated in P4.6,
  the dead `* 20[:64]` construct predates F53). Delete the dead branch or
  fix the expression.
- `src/zcode_bot/auditor.py` module docstring says "four invariants" but
  lists five. Update the count (or number them).
- `tests/test_f17_queue.py` — dead code after the `unittest.main()` call
  (never executes). Sharper (P4.6): the two `async def test_full_queue_*`
  methods are indented INSIDE the `if __name__ == "__main__":` block, so
  pytest never collects them — F51b (queue-full drop notice) has NO live
  coverage. Move them into `TestStopClearsQueue` (or a new class), don't
  delete: they assert real behavior.
- `src/zcode_bot/reasoning.py` `wait_reply(cfg, "permiso pendiente" / ...)`
  internal hint labels appear unused (pass-through identifiers asserted by
  tests but never rendered). Confirm and remove the hint parameter or the
  labels.
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
- `reasoning._poll_events` / `_vigilar_turno` are long; a split was
  declared "otro día" on 21/10.
- `deploy.sh` "ritual" greps (`grep '^def running_turns' core.py`, def-count
  of gateway) are brittle string checks — could become import-time asserts
  in a tiny `selfcheck.py` module both deploy and CI call.
- `commands/callbacks.py` carries the legacy `task:N` callback vocabulary
  forever ("old buttons still live in the owner's chats"). Give it a
  removal date once the 24h button TTL has flushed every pre-F52 button.
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
- `spectator.py` log/audit strings are now half-migrated prose:
  `"spectator: turno fijado …"`, `"spectator fin: pasos=%d"` (P6
  string pass). NOTE: the audit kind `"espectador"`→`"spectator"` is a
  PERSISTED `tipo` value (bot-audit log) — records before 21/10 keep
  the Spanish kind; any future tooling that filters by kind must
  accept both.
- Frozen survivors (byte-identical by design): the `--espectador` CLI
  flag (gateway parse, hooks spawn, usage text, test_f15 asserts), the
  `mac_espectador`→`mac_spectator` legacy knob alias in config.py, the
  `--cot-vivo`/`--cot-botones` flags and their pid/glob names, and the
  `es_run_turn` param of `auditor._orphan_caches` (Spanish stem, out
  of P5's identifier list).
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
    `cot-vivo-<sid>.pid`; `watcher_alive` reads only the new name, so a
    fresh hook may spawn a second watcher for one turn right after the
    deploy. Window: one keepalive (≤10 min), watcher processes die with
    their turns.
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
- `scripts/check_english.py --check` still exits 1 on DOCS (TECH_DEBT.md
  itself, ADR quotes, README's LEGACY_KNOBS mention, REFERENCES) —
  quotes/history in prose, deliberately NOT noqad (noqa is a .py-line
  convention). If docs must reach zero too, that needs its own policy
  decision (allowlist for quoted blocks).
- test_higiene.py::TestOnlyEnglish imports `check_english` by module
  name from scripts/ via sys.path — a generic name; if scripts/ ever
  grows another `check_english`, alias the import
  (`import check_english as _ce`) — Python would still pick scripts/
  first as inserted at position 0.
