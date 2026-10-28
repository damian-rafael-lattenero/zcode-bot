# bot — Telegram ↔ ZCode gateway

Full control of ZCode from your phone (headless: locked screen is fine).
One bot (@glm_lattenero_2_bot) to chat, ask, compact, stop and monitor.
Runtime **Python 3.13** (uv venv) + **aiohttp** (the only runtime
dependency). **985 tests, `pytest -n auto` in ~12 s** — and the ritual
runs the suite TWICE (flakiness dies before it ships).

## Architecture (post-production-refactor, Oct 2026)

```
[Telegram (phone)]
      ⇅ polling (3s long-poll)  —or—  webhook (HTTPS + secret, ADR-011)
[Gateway process — LaunchAgent com.zcode.telegram-cmds]
  └─ asyncio: runtime_async() — ONE loop for transport + supervisor
       ├─ exclusive transports: poller.AsyncPoller | webhook_server
       │    └─ hb watchdog thread (F83): the beat answers "alive",
       │       never "one more cycle done" — a slow cycle can't look dead
       ├─ dispatcher.UpdateDispatcher — serial, update_id dedup, owner
       ├─ gateway.process_update_sync — the per-update pipeline (the seam)
       ├─ commands/ — CommandRegistry + dispatch (~30 handlers, F4i)
       ├─ workers — PROCESSES (--run-turn, F16): each owns its runtime
       ├─ core — the shared seam (session, registries, stop, permissions)
       ├─ live_view.LiveTurnView — the turn's live box (F4d)
       ├─ healer.check — the channel's contract watchdog (F79c)
       ├─ zconn.listen — z.ai's down/up voice: ⚠️ lost / ✅ back (F85)
       ├─ reporting / messages / nuclear / hooks — one per domain (F4)
       ├─ state.AtomicStore + file-backed registries (flock, kqueue)
       ├─ proto.make — protocol clients (stderr per PID)
       ├─ telegram/ — rate limiter + retry + circuit breaker (F79e/F83)
       │    └─ SHARED cooldown file state/tg-cooldown (F83b): every
       │       process reads the same window — no one probes a punished
       │       chat; state/tg-last-ph (F84b): the chat's newest message
       └─ utils/ — config (paths by attribute), send/outbox, formatting
[Detached watcher family — one process per surface, pid-file owned]
  ├─ --cot-live    the thinking line (ll) + cajitas of the focused task
  │                (F41/F44; 1s blink + always-last float F84)
  ├─ --agent-live  subagents: one quiet line per agent (F63/F65)
  ├─ --agent-view  the picked subagent streams like a task (F64)
  ├─ --spectator   the Mac's own turns, live from the rollout (F33)
  └─ spawn/respawn/linger/adopt by the healer + attach (F62/F72)
[Hooks — one process per event (▶️ ✅ ❓ 🔐)]  → same package, hook mode
[ZCode runtimes — app-server --stdio (JSON-RPC)]  ← ADR-002/003
[The fork (~/Projects/ZCode, branch damian)] — the Mac's OWN build:
  flushes reasoning/tool-args mid-request to live-io-<sid>.jsonl
  (F73/F81b: the sidecar the cajitas and the preparing:N line read),
  deterministic retry pills 2^n→1024s (the curve zconn quotes)
[bot-healer — LaunchAgent, dumb by design (F79c)]
  one lstat: hb-transport stale >180s → kickstart the poller. The
  healer OF the healer: zero imports, zero network, always loadable.
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
  reasoning.py           CoT in size-based boxes with buttons (F40-F45);
                        the ll — the thinking line that blinks every
                        second and rides the bottom of the chat, the
                        cajitas, the bash outputs (F75-F86)
  attach.py              /task pick → live attach to a running turn,
                        recap + watchers ensured (F62/F72)
  agent_live.py          subagent watchers: quiet lines, respawn/adopt
  cli_logs.py            parameterizable scanner of the CLI's JSONL
                        logs (iter_events — one iterator, many truths:
                        tool_in_flight F78a, zconn F85)
  live_io.py             the fork's live-io-<sid>.jsonl sidecar reader
  healer.py              the channel's contract watchdog (F79c): turns
                        running vs watchers alive, spawn/heal ladders
  zconn.py               z.ai connection episodes → ⚠️/✅ notices (F85)
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
  telegram/circuit_breaker.py  the sync breaker + retry policy (F79e)
  utils/retry.py         the ONE retry policy of the transport (F79e)
  utils/logging.py       canonical key=value logs, death diagnostics
                        (excepthook+faulthandler), the hb heartbeat
  state/ telegram/ utils/          tested infra (F1/F2)
  models.py config.py    single catalog / single source of paths
  zcode_afterEventId.patch   runtime patch: afterEventId on session/events
src/zcode_proto.py       stdio protocol client
tests/                   pytest (unittest style): golden UX, dispatcher,
                         poller, webhook, LiveTurnView, prompt_worker —
                         80 files, 985 tests, one per behavior born
                         with it (the suite is the spec)
scripts/check_no_duplicates.py   gate: one living body per concept
scripts/check_english.py         flags any Spanish line (F53) — quotes
                         carry per-line `# noqa: spanish — reason`
scripts/bot-healer.py + .plist   the dumb healer of the healer (F79c)
scripts/bot-revive.sh            the red button (F79d) — works when
                                 the bot is ALL dead (no sends from
                                 inside a cooldown window, F83b!)
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
~/.zcode/hooks/venv/bin/python3 -m pytest -n auto -q   # the suite (~12 s, ×2)
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

## The visible pipeline (F56-F86 — the phone shows the work, always)

The standing requirement that shaped this era: **the chat must show
life at every moment** — a frozen line reads as a hang. What a turn
looks like today, top to bottom of the chat:

- **The ll — the thinking line** (`reasoning.py`): ONE message, born
  with the turn, **edited every second** (the poll's 3 s sleep splits
  into three 1 s ticks — `LL_TICK_S`): the blink pairs 🧠↔💭 /
  🔧↔⚡ / ✍️↔📝, the elapsed clock, the tool's OWN clock
  (`Running Bash · 41s`, from the CLI log — F78a), `preparing:Edit ·
  N lines` while the fork streams tool args (F81b), `writing · 1.5k
  chars` from the sidecar. It **rides the bottom ALWAYS** (F84/F84b):
  any message that lands below it — a cajita, a command reply, the
  spectator's pin — makes it delete-and-recreate itself under the
  chat's newest ph (`state/tg-last-ph`, written by every sendMessage).
  On a task switch it says ⏸️ once and freezes; the re-pick re-births
  it.
- **The cajitas** — one message per CoT box: born `filling CoT 2.1
  (300 chars)` while the sidecar streams, sealed with their button at
  the grid; tapping opens IN PLACE from the cache. Boxes that cross
  the seal grid between polls are born DONE (F78b); what a muted
  window never saw lands as ONE collapsed «📥 N cots» — **only on the
  /task re-attach** (F85a: cots arrive in the moment, never
  accumulated at a close).
- **The bash outputs** (F86): every settled Bash call sends
  `🔧 command — ✓` + the last 400 chars of its output (the pytest
  tail reaches the phone). Deduped by the part's `callID`; a respawn
  never replays the past.
- **The connection voice** (F85): the fork's retry pills, translated
  to chat — `⚠️ z.ai connection lost` with the FIRST reason and
  `attempt N/11, next retry 2^n`, then `✅ z.ai back — the cut lasted
  Ns`. One episode = one pair; sputters of the same sick wire stay
  one story; if the Mac's own internet is the corpse both notices
  park in the outbox and land together when the wire returns.

## Why headless

The bot drives ZCode the way the CLI does — `app-server --stdio`
runtimes — and never needs the app's UI:

- **The phone is the UI.** Locked Mac screen, lid closed (caffeinate
  holds the sleep), the whole surface is Telegram.
- **Every truth is a file** the headless runtime already writes: the
  rollout `model-io-<sid>.jsonl` (the spectator), the CLI's JSONL
  logs (tool_in_flight, zconn), the store's settled parts (the
  outputs), the fork's sidecars (the live CoT). The bot READS; it
  doesn't need to BE the app.
- **Headless turns know they're headless**: the runtime notice
  (`ZCODE_BOT_RUNTIME`) makes deploys arm the reload door instead of
  kickstarting (the sandbox blocks launchctl — and killing zcode
  processes would kill the very turn doing the deploy). /nuclear is
  the user's button, from Telegram, never ours.

## The armor (F79/F83 — born from a night of real incidents)

The 27/10 night left three lessons, each now a mechanism with tests:

1. **No silent deaths** (F79a): threading.excepthook + faulthandler +
   canonical key=value logs with full stacks; every loop beats
   `state/hb-<name>` per cycle.
2. **The heartbeat that answers "alive"** (F83): the hb watchdog
   thread beats every 5 s WHILE a cycle works — a cycle spending
   minutes in retries can never read as a dead process to the dumb
   healer (the 22:26 kill loop: 7 SIGTERMs of a WORKING poller).
3. **Rate limits are shared truths** (F83b/c): `state/tg-cooldown`
   (mtime = the epoch Telegram's retry_after demands) — every process
   pre-checks it; the first 429 parks (one attempt, zero sleeps —
   sleeping a server-named window inside one call is a violation that
   ESCALATES the punishment), the outbox holds everything, one burst
   lands when the window lifts. Under a real storm (118 sends in 3
   min, F86b) this armor took zero 429s.

## Design patterns (the ones the code leans on)

- **Facade + composition root** — `gateway.py` wires every dependency;
  loops receive callables, never import behavior (ADR-012).
- **Strategy** — `cli_logs.iter_events`: one iterator, parameterized
  filters, many callers (tool_in_flight, zconn, housekeeping).
- **Pure core + injectable side effects** — every decision function
  (`_ll_should_float`, `ConnTrack.feed`, `_bash_outputs`,
  `healer.evaluate`, `_pill_vocab`) is pure and takes the clock/
  iterators it needs; the tests pin behavior without I/O. The poller
  takes `fetch/process/healer_check/conn_check` as parameters.
- **Episode state machines** — zconn's ConnTrack, the healer's
  ladder, the breaker's CLOSED/OPEN/HALF_OPEN: transitions ARE the
  behavior, so the tests ARE the spec.
- **File-as-truth with mtime semantics** — `tg-cooldown` (mtime =
  expiry), `tg-last-ph`, `hb-*`, the pid-files: cross-process state
  with zero locks and one lstat.
- **Dumb-by-design outer ring** — bot-healer reads ONE mtime; the
  healer of the healer must survive the package being broken.
- **Registry + table-driven** — commands, callbacks, LEGACY_KNOBS.
- **Reactive waits, never sleep-polls** (ADR-009) — and in the shell:
  kqueue vnode waits for file events, `tail -f | sed /PAT/q` for log
  events; a plain `&` dies with the shell's SIGTERM — detached work
  uses `start_new_session`.

## Testing

- **985 tests, `pytest -n auto -q` in ~12 s** on this Mac (all
  cores); the ritual is **×2** — two full runs before any commit.
- Gates in the same ritual: `check_english` (Spanish only with
  per-line noqa), `check_no_duplicates` (one symbol one body),
  `selfcheck`, mypy clean.
- Style: unittest classes, `support.fresh` isolates STATE_DIR per
  test, patches ALWAYS at the owning module
  (`mock.patch.object(zc_http, "send")`). `pytest-timeout` 60 s.
- Live verification ≤1 min after deploying anything that sends —
  the storm of F86b was caught by exactly this discipline.

## The ZCode fork connection (~/Projects/ZCode, branch `damian`)

The Mac runs its own build of ZCode from source (never patched
binaries — signing kills updates silently). The bot and the fork
grew together:

| Fork change | What the bot does with it |
|---|---|
| `live-io-<sid>.jsonl` sidecar: reasoning/text chars flushed mid-request (F73) | the cajitas fill LIVE; the ll's ✍️ writing state with real char counts |
| tool-args streaming (`toolName/toolChars/toolTail`, F81b) | `⚡ preparing:Edit · N lines` — the line counter of the Mac's UI, in the chat |
| deterministic retry curve 2^n→1024s (f85a67f6) | zconn's «attempt N/11, next retry 2^n» — the same vocabulary as the pills |
| `afterEventId` runtime patch | incremental history polling (the 3.1GB OOM never came back) |
| Diff Ask card / Changes row | the `/diff` command's home surface |
| `build-install.sh --yes / --when-quit / --zip` | deploys of the app side: unattended, interrupt-proof (a detached python waiter that survives the shell) |

Installing the fork: `scripts/build-install.sh --keep-prev --yes --zip
<path>` (the previous app survives at ZCode.prev.app). The bot repo
deploys itself with `./deploy.sh`; the fork with its installer —
never the twain.

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
