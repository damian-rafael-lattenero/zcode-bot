# bot — Telegram ↔ ZCode gateway

Full control of ZCode from your phone (headless: locked screen is fine).
One bot (@glm_lattenero_2_bot) to chat, ask, compact, stop and monitor.
Runtime **Python 3.13** (uv venv) + **aiohttp** (the only runtime
dependency). 659 tests, parallel suite in ~45 s.

## Architecture (post-production-refactor, Oct 2026)

```
[Telegram (phone)]
      ⇅ polling (3s long-poll)  —or—  webhook (HTTPS + secret, ADR-011)
[Gateway process — LaunchAgent com.zcode.telegram-cmds]
  └─ asyncio: runtime_async() — ONE loop for transport + supervisor
       ├─ exclusive transports: poller.AsyncPoller | webhook_server
       ├─ dispatcher.UpdateDispatcher — serial, update_id dedup, owner
       ├─ gateway.process_update_sync — the per-update pipeline (the seam)
       ├─ commands/ — CommandRegistry + dispatch (~30 handlers, F4i)
       ├─ workers — PROCESSES (--run-turn, F16): each owns its runtime
       ├─ core — the shared seam (session, registries, stop, permissions)
       ├─ live_view.LiveTurnView — the turn's live box (F4d)
       ├─ reporting / messages / nuclear / hooks — one per domain (F4)
       ├─ state.AtomicStore + file-backed registries (flock, kqueue)
       ├─ proto.make — protocol clients (stderr per PID)
       ├─ telegram.AsyncTelegramClient — rate limiter + circuit breaker
       └─ utils/ — config (paths by attribute), send/outbox, formatting
[Hooks — one process per event (▶️ ✅ ❓ 🔐)]  → same package, hook mode
[ZCode runtimes — app-server --stdio (JSON-RPC)]  ← ADR-002/003
```

Central invariant: **ONE dispatcher processes updates**
([ADR-011](docs/adr/ADR-011-webhook-transport-dispatcher.md), which
supersedes
[ADR-001](docs/adr/ADR-001-single-getupdates-reader.md)). Two writers
(app ↔ bot):
[ADR-003](docs/adr/ADR-003-two-writers.md). /stop only stops turns of
its own runtime:
[ADR-002](docs/adr/ADR-002-stop-from-same-runtime.md). The polling and
webhook transports are mutually exclusive. The gateway is a pure facade
(composition root, exactly 3 definitions of its own —
[ADR-012](docs/adr/ADR-012-dismemberment-by-delegation.md): dismemberment
by delegation, move without copying).

## Layout

```
src/zcode-telegram.py    shim (entrypoint for the LaunchAgent/hooks/subprocesses)
src/zcode_bot/           the package:
  gateway.py             facade: process_update_sync + runtime_async + main
  core.py                shared seam (session, _workers()/_questions(),
                         stop markers, permission cache, queue/turn reads)
  messages.py            ProtoClient parsers (entity: ToolStep)
  live_view.py           LiveTurnView + anti-OOM guards + _turn_end_poll
                         (F7 live action: tools 🔵→✅ live, afterSeq)
  reasoning.py           CoT in size-based boxes with buttons (F40-F45)
  spectator.py           the Mac's turns live on Telegram (F33)
  acks.py                the «your choice arrived» ack (F36)
  limits.py              constants shared across modules (CH_TURN /
                         CH_REPLY / CH_BOX, TELEGRAM_MSG_MAX, PARTES_MAX)
  nuclear.py             ☢️ Nuclear 2.0 (entity: NuclearReport)
  reporting.py           reports and deliveries (entity: Task)
  workers.py             queue + spawn + prompt_worker (QueueItem, WorkerEntry)
  updates.py             Telegram plumbing (offset, busy, _cb_text, polling)
  hooks.py               the CLI's on_* entrypoints (wait_reply via commands)
  commands/              registry.py (CommandRegistry) + dispatch.py (~30
                         handlers, F4i) + handlers by domain: handlers_tasks
                         / handlers_info / handlers_system / handlers_diff;
                         callbacks.py (button vocabulary, R5), args.py (R7)
  dispatcher.py/poller.py/webhook_server.py   asyncio core (F2/F3)
  audit.py/auditor.py    shared failure log + invariant watcher (F30)
  state/ telegram/ utils/          tested infra (F1/F2)
  models.py config.py    single catalog / single source of paths
  zcode_afterEventId.patch   runtime patch: afterEventId on session/events
src/zcode_proto.py       stdio protocol client
tests/                   pytest (unittest style): golden UX, dispatcher,
                         poller, webhook, LiveTurnView, prompt_worker — 57 files
scripts/check_no_duplicates.py   gate: one live body per concept
scripts/check_english.py         flags any Spanish line (F53)
scripts/check_ast_equal.py       proves a rewrite is comment/docstring-only
deploy.sh                tests → gate → backup → copy → ritual → door
bootstrap.sh             runtime from scratch (uv + 3.13 + venv + deps)
requirements(.dev).txt   pinned deps (runtime: aiohttp; dev: pytest+xdist+timeout)
docs/adr/                18 ADRs (001 superseded by 011, 004 by 010)
docs/runbook.md          operations, runtime, transport, incidents
```

## Daily usage

```bash
./bootstrap.sh                       # prepare/repair the runtime (idempotent)
./deploy.sh                          # full deploy (parallel tests + import gate)
./deploy.sh --rollback               # restore the previous version
~/.zcode/hooks/venv/bin/python3 -m pytest -n auto -q   # the suite (~45 s)
alias py3="$HOME/.zcode/hooks/venv/bin/python3"        # shortcut (in ~/.zshrc)
```

Bot commands: `/help` in the chat (menu scoped to the owner). `/stop`
brakes cooperatively and **escalates on its own**: if the runtime
doesn't die within 4 s, it kill -9s the group (worker + app-server);
`/stop go` kills instantly.

## The live turn (F7 — live action)

While a turn runs, the chat shows EVERYTHING the agent does:

- **Live box**: the current CoT (thinking / writing) with the current
  tool beside it — the command is seen **typing itself** while the model
  generates it (`input_streaming`) and then `🔵 running`.
- **Per-tool box**: as soon as the input completes, a box with the full
  command / real diff (`_tool_box`) travels to the chat; it is born `🔵`
  and is **edited** to `✅`/`❌` with the output when the result arrives.
- **CoT as buttons** (F42/F43): the reasoning is sealed into SIZE-based
  boxes during the thinking pass — one button per box as soon as it
  fills, numbering restarting on every pass. Tapping a button
  (`ct:…` → `/cotb`) opens the box instantly from the cache — no
  runtime. The same model feeds the three sources: the phone
  (LiveTurnView), the live Mac watcher (`--cot-live`, F41/F44) and the
  close-time capture (`--cot-buttons`, F40). A turn that closes WITHOUT
  reasoning says so in one line (F45).

Knobs: `cot_stream` / `tools_stream` (default ON), `cot_buttons`,
`cot_empty_notice`, `live_progress`, `tools_box_filter`
(`action` | `all` | `minimal`).
Infra: **incremental** event polling (`afterSeq`, initial cursor from
the subscribe's `eventSeq` — the full replay once inflated a real turn
to 3.1GB RSS); anti-OOM guards as a safety net; stream↔history pairing
by `callID`/`toolCallId`. Details: ADR-013.
Each Edit's diff carries a diffstat on top: ● 🟢 +added  🔴 −removed
(Damian's request — the total at a glance before the line-by-line
detail; MultiEdit repeats it per edit).

Fast start: the placeholder «starting…» goes out in ~2 s (before
touching the runtime) and no-op sets are skipped (`setModel`/`setMode`
return the FULL history per request — 26MB ≈ 30 s on a large session;
the model is validated against the sqlite, the mode against
`mode_set`). A very large session still pays the `resume`: the
**rotation** (below) avoids it.

## Instant reply (F8 — warm worker + rotation)

The pattern of the products that answer instantly: **state stays
alive** and **sessions are short**
([ADR-014](docs/adr/ADR-014-warm-worker-rotation.md), with sources).

- **Warm worker**: after delivering the answer, the runtime does NOT
  die — it waits for that session's next message for up to
  `warm_keepalive_s` (default 600; `0` = one process per turn, as
  always). Back-to-back messages start **instantly**: zero resume,
  zero «starting». The heartbeat renews the registry entry
  (waiting=True) and `/stop` brakes it in <1 s without the kill.
  While it waits, the app's model picker shows the session with
  zai-bot (window bounded by the keepalive — the same state as today
  during a long turn).
- **Automatic rotation**: if the session's transcript
  (`cli/rollout/model-io-<sid>.jsonl`) exceeds `rotate_threshold_mb`
  (default 8; `0` = off) when it goes warm with an empty queue, the
  dance runs: `compact` → a summary turn → a **seeded new session**
  with that summary → swap of the active session (registry and queue
  rekeyed; the old one stays archived, reachable via `/task`). The
  compact does NOT shrink the transcript (measured: 4 compacts and the
  resume was still at 27.5MB) — rotation does: the cold start goes
  back to ~5 s forever. Any step that fails → notice and continuity in
  the old session (the thread is never lost). Transcripts of sessions
  no longer referenced are purged after `rollout_retain_days`
  (default 7, `0` = off; F9 — the housekeeping: each rotation used to
  leave 44MB behind).

## Event-based turn end (F9 — ADR-015)

`_turn_end_poll` no longer scans the CLI's JSONL logs as its main
path: the end arrives via the **stream** `session/events` (incremental
`afterSeq` poll, KBs). The stream carries no `turnId` — the F20
correlation is rebuilt from `inputId` (spike
`scripts/spike_f9_turnfin.py`; decision gate). The subscribe happens
BEFORE the `send`, always, so the cursor guarantees the turn's events
come in. Any stream failure (dead conn, frame >25MB) degrades 1:1 to
the log scan, which stays as the net (and remains the only path of the
orphan supervisor). Permissions were already push
(`interaction/requestPermission`). Close-out of ADR-013's follow-up;
`commands/dispatch.py` split into `handlers_tasks` / `handlers_info` /
`handlers_system` (ADR-012 debt) — later plus `handlers_diff` — with
`support.patch` + the anti-plain-rebinds gate (`test_higiene`).

## The invariant auditor (F30 — ADR-016)

The boundary watcher that was missing: orphans_check, stuck-watch and
the outbox are **intra**-component self-checks; the auditor watches the
seams between components as a black box, via files, no locks, no hot
path. It runs in the poller's supervisor with its own throttle
(`auditor_interval_s`, default 30; **0 = off**). Five invariants, all
born from real incidents:

1. **Failure correlation** — `≥ auditor_failures_min` (default 2)
   failures of the same type+session within 10 min → «sick session,
   /task to another one or wait for rotation». Components log their
   failures to `state/bot-audit.json` (`audit.registrar`: proto, send,
   turn-failed, crash, runtime, handler, spectator…); the global bucket
   covers send bursts (the 429/deadlock of 15/10 15:47 would have
   alerted on its own). With this, the 18:10 incident is detected on
   the second attempt, not the fourth.
2. **Orphan /stop markers** — with no worker or turn that should own
   them (and older than 60 s, anti-race with spawn): silently
   consumed. The nuclear SIGTERM creates the marker in the handler and
   the subsequent SIGKILL prevents consuming it → it was stopping the
   next turn on the rebound (18:13, worker 13066).
3. **Ghost workers** — a live run-turn with no registry entry → alert
   with /stop go (empty registry = /stop, /status and the stuck-watch
   blind, 15:40 incident). A recommendation, not a kill.
4. **Sleeping queue** — items queued for >10 min with no worker and no
   run in flight → «/qgo or /task». The normal F17 flow (Mac busy) does
   NOT alert; legacy items without `ts` don't either.
5. **Orphan CoT caches** (F51a) — a `live=True` cache of a turn that
   FINISHED with no watcher or worker to close it (the watcher died
   midway): closed idempotently — the buttons keep working, only the
   live flag is cleared.

Anti-spam: one alert per key every `auditor_alert_cooldown_min`
(default 60). Philosophy: **heal what is idempotent, recommend what is
state-dependent** — it never runs destructive actions. Bonus: the
poller now reaps its child Popens every cycle (end of the `<defunct>`
zombies, debt from the 18:10 incident).

## An honest /ctx (F31)

The 15/10 e2e left a false negative: `/compact` had worked (the
runtime logged `compact.completed`, 185k → 23k tokens on the next
request) but `/ctx` kept showing 185k. Two causes, both fixed:
(1) the `ctx_probe.py` probe did `session/resume` with a fixed 20 s
timeout (the same bug class as F29) and besides, the runtime does
**not persist** the projection — `contextUsed` was always 0, so /ctx
never showed a measured number and paid a 35 s / 3 GB resume per
query; (2) `_context_ktokens` read a 64 KB window of the queue, but
each record of the model-io jsonl weighs as much as the context
(200 KB+), so the parse failed exactly on the large sessions and
`/compact` fell to the base timeout. Now `core.last_used(sid)` reads
the rollout with a growing window and returns `(ktokens, epoch,
source)`; `/ctx` shows that number in 0.05 s and, if the last
`compact.completed` is newer than the last request, it says
«🧹 compacted HH:MM — the real size shows on the next turn (before
N)» instead of repeating the stale number. The probe is retired.

## The Mac's turn arrives complete (F32)

Damian's request: the notices of turns run from the Mac app used to be
previews (200 chars of prompt, 500 of answer). Now the hooks send the
**full text** chunked to Telegram's limit (chunks of 3800, up to 6
parts; if it exceeds, the last part marks the cut — the full text
stays in the session, `/cot`). The header carries what matters: task
name (🧵, from the task index; workspace as fallback), origin (🖥️
Mac), steps (🧰 `toolCallCount`), duration (⏱, the marker's mtime)
and workspace (📁). One single notification: first part with sound,
the rest silent. Gates intact: `notify_prompt`/`notify_stop`, F15 (a
session with a bot worker doesn't duplicate) and `_task_current_ok`.
+6 tests.

## The Mac's turn live: the spectator (F33 — ADR-017)

F32 sends the start and end of turns run in the Mac app; F33 adds
**the middle**: a detached process (the «spectator», spawned by
`on_user_prompt` alongside the stuck-watch) does an incremental tail
of the rollout `model-io-<sid>.jsonl` and sends to Telegram, in the
usual format, every step of the model: tool call box (full command /
real diff), intermediate narration, and the initial label
«🖥️ Mac turn live (spectator)». The final answer is NOT sent by the
spectator: the F32 Stop hook delivers it (the spectator shuts down
when the marker is consumed — zero duplication by construction).

Why not the event stream: it belongs to the client handling the turn —
`session/subscribe` on an app session answers «Session is not active»
(spike 19/10). The rollout is the same file F31 reads for /ctx: one
record per completed request, no runtime, no protocol. Honest limits:
per-step granularity (no live typing) and **no CoT** — the runtime
does not persist the thinking blocks, neither in the bus nor in the
rollout. The knob `mac_spectator` turns it all off;
`tools_stream`/`tools_box_filter`/the F15 gate keep applying.
+17 tests.

## An e2e log (what a turn looks like from the phone)

A real turn from 15/10/2026 (commit `1aa9620`, warm worker active) —
what Damian saw in the chat, step by step:

1. **Message from the phone**: «ask me a question, work according to
   the answer, run a command and modify the readme».
2. **Instant box**: 🧠 «testing» starting… → opening the session →
   session ready — sending… (real progress, not a statue).
3. **Live CoT**: the reasoning arrives while the turn runs — numbered
   boxes, one button per sealed box (with `cot_buttons`; the text of
   each opens on tap), interleaved with the live box of the current
   tool.
4. **❓ interactive question**: the agent asked what to add to the
   README with 3 options — Damian answered by tapping the option on
   the phone (`AskUserQuestion` → ❓ headless → the answer goes back
   to the runtime).
5. **Visible tools**: a 🔵 box per tool that **does** something
   (`action` preset) with the command typing itself live, edited to ✅
   with the output; Read/Grep only in the step index (bookkeeping
   stays out of the way).
6. **git-diff style**: the README edit arrived as a box with the
   diffstat `● 🟢 +N 🔴 −N` on top and the 🟢/🔴 lines below, one per
   line.
7. **Delivery**: ✅ «testing» · reply: + the agent's final summary.

In that turn the worker stayed **warm** (F8): Damian's next message
showed no «starting» — it began instantly on the same runtime.

## Knobs: what can be tuned (F34, ADR-018)

Every knob lives ONLY in `DEFAULTS` (config.py) and changes without a
deploy by editing `~/.zcode/telegram-remote.json`. Values shared
between modules live in `limits.py`; domain ones, in their module. The
gate (`scripts/check_no_duplicates.py`) enforces: one symbol one body,
`config.X` must exist, and no unnamed semantic literal outside the
allowlist. Legacy Spanish key names (`rotar_umbral_mb`,
`rollout_retain_dias`, `auditor_cada_s`, `auditor_fallos_min`,
`auditor_alerta_cooldown_min`, `mac_espectador`, `mac_cot_botones`,
`cot_vivo_keepalive_s`, `cot_aviso_vacio`, `cot_botones`,
`tools_caja_filtro` — and its `todas`/`accion`/`minimo` values) are
still accepted as aliases (`LEGACY_KNOBS` in config.py).

| Knob | Default | What it does |
|---|---|---|
| `wait_timeout_s` | 86400 | how long the chat's answers are waited for (permissions, /deploy) — phone buttons wait 24 h (F52) |
| `question_wait_timeout_s` | 86400 | wait for AskUserQuestion in phone mode (F52) |
| `question_mode` | mac | a question: `mac` notifies and answers on the Mac; `phone` waits for the phone |
| `stuck_after_min` | 20 | minutes of a stuck turn before the notice |
| `loud_all` | True | notifications with sound unless loud=False explicitly |
| `suppress_error_patterns` | 5 regex | failures that don't notify (known noise) |
| `notify_prompt` / `notify_stop` / `notify_failures` / `notify_session` | True | Mac hook notices (F32/F15) |
| `only_active_task` | True | only the active task pushes to the chat (F15) |
| `warm_keepalive_s` | 600 | seconds a warm worker waits (0 = one process per turn) |
| `rotate_threshold_mb` | 8 | MB of rollout that trigger a session rotation |
| `rollout_retain_days` | 7 | days of retention for old rollouts |
| `auditor_interval_s` / `auditor_failures_min` / `auditor_alert_cooldown_min` | 30 / 2 / 60 | the auditor's cadence and sensitivity (F30; 0 = off) |
| `mac_spectator` | True | live streaming of the Mac's turns (F33) |
| `mac_cot_buttons` | True | the CoT of Mac turns LIVE: a «thinking…» box whose keyboard grows one button per box; at close, the final text (F40/F41) |
| `cot_live_keepalive_s` | 600 | seconds the Mac's CoT watcher stays WARM waiting for the next prompt of the same session — one resume per session, not per turn (F44; 0 = one watcher per turn) |
| `cot_stream` / `tools_stream` / `tools_box_filter` / `live_progress` | True / True / action / True | what a phone turn streams (F7) |
| `cot_buttons` | True | the phone's CoT as buttons that grow per thinking pass (replaces cot_stream's text — F42) |
| `cot_empty_notice` | True | a turn that closes WITHOUT reasoning says so in one line «no CoT» (phone, live Mac, capture at close). GLM-5.3 reasons adaptively — it decides per call and often skips it with a very long context; Flash reasons almost always (F45) |
| `trace_documents` | False | /cot saves the turn's document |
| `headless_guard` | True | anti-suicide guardrail on headless prompts |
| `transport` | polling | updates transport (webhook requires ADR-011) |
| `webhook_url` / `webhook_secret` | "" / "" | written by /set_webhook |

## House rules

- **Comment tags** — comments carry provenance tags (`# F42: …`,
  trailing `(F51a)`): the prose next to a tag must stand on its own;
  the tag only dates and greps the change. All tags are decoded in
  [docs/F-CODES.md](docs/F-CODES.md).
- **One live body per concept** — enforced by the anti-dup gate in CI
  (the lesson of the 14/10 refactor that left HEAD unable to import).
- **Everything by attribute, nothing baked at import time**:
  `config.STATE_DIR`, `_workers()`, `uhttp.send`
  ([ADR-012](docs/adr/ADR-012-dismemberment-by-delegation.md)). The
  tests' patch seam is ALWAYS the owning module:
  `mock.patch.object(zc_http, "send")` — a rebind in the wrong
  namespace is silent and the test stops testing.
- **No sleeps for coordination**
  ([ADR-009](docs/adr/ADR-009-reactive-async-tests.md)): reactive waits
  with TTL; pytest-timeout 60 s per test.
- Move, don't copy: every extraction lands with a green suite in the
  same commit; moving and changing representation (dataclasses) go in
  separate commits, with round-trips of the state JSONs.

## Runbook

See [docs/runbook.md](docs/runbook.md) (runtime, webhook/polling
transport, incidents).
