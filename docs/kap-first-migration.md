# KAP-first: the road from the Python bot to a Kotlin bot on KAP

Decision doc and execution roadmap. Written 2026-10-28.
Status: direction approved; execution not started; K0 (KAP 4.2 `Process`)
is the first move.

Companions: `kap` @ 4.1.0, `agent-brain` @ P0-P6, and this repo's 20 ADRs.
Every number in this doc was verified against the working trees the same
day (appendix).

## 0. TL;DR

- The bot works. 64 modules, 17,625 LOC, 1,012 tests, one runtime
  dependency (aiohttp), and armor earned through ~90 incident tags. This
  is not a rescue migration; it is a thesis play with robustness
  dividends.
- The thesis: KAP (the orchestration library), agent-brain (RAG, memory,
  evals, MCP), the forked harness (ZCode), and this bot are one system.
  Today the bot is the only piece that does not run on KAP. Moving it
  there closes the loop: the library that powers the AI platform also
  powers the live agent interface.
- The order matters and it is KAP-first: SPEC-5.0's dogfooding contract
  already names this bot's live turn viewer as the acceptance surface for
  KapStream 5.0, and 4.2's `Process` is designed by this bot's own heal
  architecture. Building those before migrating means the migration
  consumes law-tested features instead of writing disposable hand-rolled
  Kotlin.
- Execution: a strangler migration through the existing file contracts
  (flock, mtime clocks, tmp+fsync+rename), phases K0 then B0..B5, each
  green and deployed alone with a rollback door. No big bang, no feature
  work during migration.

## 1. The vision: five pillars, one thesis

The AI engineer stack people describe in talks: a harness you control,
retrieval, agent memory, tools/MCP, and evals. Mapping it to what exists:

| Pillar | Here | Status |
|---|---|---|
| Agent harness | fork of ZCode (branch `damian`): live CoT flush (F73), Diff Ask anchoring | deployed, daily driver |
| RAG | agent-brain P2: hybrid HNSW cosine + tsquery with RRF fusion, recall gate | green |
| Agent memory | agent-brain P3: LLM extraction with strict schema, salience decay, cited injection (ADR-020) | green |
| Tools / MCP | brain-mcp: `rag_query` / `memory_search` wired into ZCode | green |
| Evals | agent-brain P6: EvalRunner CLI, recall gate in CI, 20/20 | green |
| The seam: event bus | ADR-019: JSONL append-only, dedup downstream, at-least-once | green |

The missing link is the transport: the bot that ties the harness to the
phone, the piece a human touches every day. It runs on hand-rolled Python
armor (threads, asyncio-over-executor, subprocess herds, three heal
layers). The migration puts it on KAP, which makes the portfolio argument
concrete: not "I use AI tools" but "I build the orchestration substrate my
AI systems run on, and here is the production proof".

## 2. Why KAP-first (not bot-first)

SPEC-5.0's dogfooding contract is explicit (kap, `SPEC-5.0-concurrency.md`,
"Dogfooding contract"):

> Every milestone ships used, not just tested: 4.2's `repeat` replaces the
> four hand-rolled loops of agent-brain (...) and `awaiting` replaces its
> process-park; 5.0's `KapStream` must power at least one real surface
> (the live turn viewer of the bot, or the event tailer as a stream)
> before it is called done.

Three consequences:

1. If the bot migrates first, it must hand-roll supervision in Kotlin
   (restart loops, heartbeats, watchers) that KAP 4.2 would then replace.
   That is double design for one problem. If `Process` lands first, the
   migration consumes it from phase B1 onward.
2. `Process`'s design brief already exists, written by this bot's
   incidents: heartbeat must be independent of cycle progress (F83: the
   beat only fired at cycle end, a cycle parked in 429 sleeps read as
   dead, and the outer healer killed a healthy poller seven times);
   restart policies must converge (never a hot loop); the dumb outer ring
   stays dumb (bot-healer is dependency-free and network-free by design).
3. Every KAP milestone ships with a production user. That is the
   library's own quality bar, and the bot is the most demanding user
   available: multi-process file contracts, a hostile rate limiter, and a
   human waiting on the other end.

## 3. What KAP already solves (the incident-driven mapping)

Each row is armor this repo built by hand after an incident, and the KAP
4.1.0 API that expresses the same idea today, with laws and property
tests behind it.

| Armor in the bot (today) | Provenance | KAP 4.1.0 |
|---|---|---|
| `retry_call` (utils/retry.py:23): backoffs (0,1,3), `retry_after` override + cap, `should_retry` fail-fast, injectable jitter | F79e, F83 | `retry(Schedule)` with `jittered()`, `shouldRetry`, `retryOrElse` fallback |
| `SyncCircuitBreaker` (telegram/circuit_breaker.py:99): CLOSED/OPEN/HALF_OPEN, getUpdates as the half-open probe | F79e | `CircuitBreaker` with `onStateChange` (the health line subscribes); cancellation never counts as failure |
| MB-scaled timeouts on setModel/setMode; the general lesson that timeout must compose with retry | 15/10 18:10 | `timeoutAsFailure` (retryable `KapTimeoutException`; KAP's own F-02, found in dogfood) |
| hb watchdog thread independent of the cycle (poller.py:22) | F83 | `repeat(spaced)` as a sibling coroutine; K0 `Process` heartbeat makes it structural |
| Watcher cadences: stuck-watch, spectator, agent-live loops | ADR-017, F62 | `repeatWhile` / `repeatUntil` / `Kap.awaitUntil(every, upTo)`, cancellation honored between iterations |
| Workers' finally-cleanup: registry, active, stop-marker, model restore | F58 ghosts | `bracket` / `bracketCase` / `Resource`: release runs in NonCancellable, ExitCase commit/rollback |
| Turn-end: stream event vs log-scan fallback | ADR-015 | `race` / `firstSuccessOf` (first success wins, failures accumulate suppressed) |
| "The bus tap never breaks a turn" (bus.py) | ADR-019 | `settled()` / `traverseSettled()`: partial failure without canceling siblings |
| ProtoClient `Condition` + locks for request/response over stdio (zcode_proto.py) | F73 era | `Channel` for stdin writes, `Flow` from the reader thread, `Kap.awaiting { resume, cancel }` for onClose bridges |
| Warm handles, cancel-safe caching | ADR-014 | `memoize` / `memoizeOnSuccess` (cancellation never poisons the cache) |
| A thread that dies silently leaves "no trace anywhere" | F78 | structured concurrency: children live in scopes; the isolation law (`isolated()`) is property-tested; one central CoroutineExceptionHandler |

The gaps that do NOT exist yet, and what they become here:

- `Process` (KAP 4.2): a repeated Kap with restart policy, health,
  heartbeat, and a stop handle. Design source: this bot's three heal
  layers. That is milestone K0 below.
- `Supervisor` (OneForOne/AllForOne) and `KapStream` with named
  backpressure strategies (buffer/conflate/dropOldest/latest(1)):
  milestone 5.0. The live turn viewer is the declared acceptance surface,
  and the 118-send storm (F86b) is the case study for conflate semantics.
- `Ref` / `Queue` / `Promise` thin wrappers: explicitly deferred by
  SPEC-5.0; raw kotlinx.coroutines until they exist.

## 4. What the language and the ecosystem add

### 4.1 The type system as a gate

- mypy today is report-only (mypy.ini, CI `continue-on-error`). In Kotlin
  the same checks are the compile step: the 94 annotated public
  signatures become whole-program inference, and the ~20 persisted
  formats of schemas.py (TypedDicts with bilingual legacy keys) become
  `@Serializable` data classes with the legacy mapping at one adapter
  boundary.
- The repo is full of episode machines: `ConnTrack.feed` (zconn.py:56,
  first-reason-wins), the watcher state matrix (F81), the CoT box states,
  bot_mode DEFAULT|writer. As dicts with string keys, adding a state
  silently misses handlers. As `sealed interface` + exhaustive `when`,
  the code does not compile until every state is handled.
- The closure-without-nonlocal bug class (14/10, "Probando cot 1234")
  disappears under `val` + `copy()`.

### 4.2 One async model instead of two

The production send path is synchronous urllib (utils/http.py) while an
async aiohttp client with its own rate limiter and per-endpoint breakers
exists for webhook operations (telegram/client.py). Two transports, two
retry stories, two breaker families. In Kotlin: one Ktor client with KAP
policies around it. The `asyncio.to_thread` bridges go away, and
`contextvars` (bind_context) map to CoroutineContext + MDC, where KAP's
own `context()` operator can read them.

### 4.3 Forensics: the F78 class stops being possible

The 27/10 14:25 silent watcher death "left NO trace anywhere"; the death
diagnostics in utils/logging.py exist precisely because Python gives you
nothing by default. The JVM gives, out of the box:

- jcmd/jstack thread dumps of a live daemon: "what is the watcher doing"
  answered without killing anything.
- JFR (flight recorder) always-on at ~1% overhead: stalls, lock
  contention, GC pauses, in production.
- `-Xmx` as a hard ceiling plus `-XX:+HeapDumpOnOutOfMemoryError`: the
  3.1-3.5 GB OOM era (unbounded session replay) would have been a dump
  file and a guardrail, not a mystery.
- Logback with MDC (sid/turn/component, the same fields bind_context
  carries) and size-capped rotation: the 294 MB errors.log incident gets
  a policy, not a habit.
- Micrometer counters/gauges behind /status: cooldown remaining, breaker
  states, outbox depth, watcher ages. Today those are strings assembled
  by hand.

### 4.4 The stack (heavy on purpose, bounded on principle)

| Concern | Choice | Replaces |
|---|---|---|
| Orchestration | kap-core + kap-resilience (kap-arrow only if parallel validation earns it) | the hand-rolled armor above |
| Concurrency | kotlinx.coroutines 1.11 (through KAP) | threads + asyncio + executor bridges |
| HTTP client | Ktor 3.6 client (retries, timeouts, pooling; already a KAP-family dependency) | urllib + aiohttp |
| Webhook server | Ktor server (CIO or Netty) + kap-ktor's respondKap | aiohttp webhook_server |
| Serialization | kotlinx.serialization, strict mode + legacy adapters | stdlib json + schemas.py |
| Logging | kotlin-logging over Logback, MDC, size-rolling | the custom logging module |
| Metrics | Micrometer + Prometheus registry at /metrics | none |
| Caches | Caffeine (update-id dedup TTL, permission fingerprints 10 min, CoT boxes) | OrderedDict TTL, plain dicts |
| SQLite | xerial sqlite-jdbc (tasks_repo reads, _restore_model writes) | stdlib sqlite3 |
| Files | NIO WatchService (kqueue-backed on macOS, the same primitive fswatch.py arms) exposed as a Flow; FileChannel.lock for flock; Files.move ATOMIC_MOVE + channel.force for fsync | fswatch.py, AtomicStore internals |
| Subprocesses | ProcessBuilder under bracket + awaiting (app-server, caffeinate -w) | the Popen sprawl |
| Tests | JUnit 5 + kotest-property + kotlinx-coroutines-test virtual time + kap-kotest matchers | pytest + injected clock/sleep fakes |
| Build | Gradle application plugin (dist + launcher scripts), detekt | deploy.sh's rsync step (deploy.sh keeps its gates) |

Explicit non-goals: Spring/Micronaut (startup weight, nothing we need),
Akka (rejected in SPEC-5.0: no clustering, no location transparency),
Reactor/RxJava (Flow is the runtime), STM (rejected), DI frameworks (the
composition-root/facade style of gateway.py stays manual).

### 4.5 The cross-repo seam disappears

brain_client.py (109 LOC) and bus.py (90 LOC) are the only
Python-to-Kotlin bridges in the system. The bus event contract
(id/ts/kind/sid/turn/source/payload) already exists in Kotlin inside
agent-brain (FileTailer, PgBusIngest). After B2/B3 the bot consumes the
brain's Kotlin contracts directly: one schema, one language, and the
JSONL/Kafka seam becomes an internal interface instead of a protocol
boundary.

### 4.6 Honest performance note

JVM baseline RSS will be higher than the Python poller (hundreds of MB
vs tens). JSON parsing is faster and real threads help the render core,
but the historical OOMs were architecture bugs (unbounded replay), not
language limits. The JVM adds hard ceilings and post-mortem dumps; it
does not add virtue by itself.

## 5. Honest costs

1. The render core is the risk: reasoning.py (1,931) + agent_live.py
   (1,800) + live_view.py (1,144), about 4,875 LOC of in-place message
   editing where most incidents lived. It is the last migration phase
   (B4) for a reason, and it gets KapStream as its substrate rather than
   a port.
2. The 1,012 tests do not port; they get rebuilt. The style ports cleanly
   (pure decision functions + injected effects map to pure functions +
   virtual time). The armor is rebuilt as property tests over the state
   machines plus golden replays of recorded production traces (B0 builds
   the recorder).
3. JVM startup (~1s cold) rules out Kotlin for the per-event hook
   processes. Hooks stay thin: the Python shims remain, or become a tiny
   client posting to the daemon's unix socket. Everything long-running
   belongs to the daemon.
4. Deploy gets heavier: a versioned artifact instead of rsync of .py
   files. deploy.sh keeps its shape (gates, backup, install, reload
   door); the artifact is one directory with a launcher.
5. The strangler window: two runtimes coexist for months. Safe because
   the file contracts are already the inter-process interface (they
   coordinate Python processes today); risky because each phase must
   honor them exactly (flock deadline semantics, mtime clocks, bilingual
   keys, offset confirmed post-dispatch). The golden replay harness
   exists to enforce byte-level parity.
6. KAP 4.2/5.0 do not exist yet. K0 is real library work with laws and
   benchmarks, and it is on the critical path. That is the point, and it
   is also the schedule risk.

## 6. The roadmap: K0, then B0..B5

Every phase: tests + gates green, replay parity where applicable,
deployed alone, rollback door verified. No phase starts before the
previous one is deployed. F-code discipline applies to each.

| Phase | Repo | Ships | Exit criteria |
|---|---|---|---|
| K0 | kap | 4.2 `Process`: restart policies (constant/exponential, maxRestarts), heartbeat, stop handle, convergence laws | laws + virtual-time tests; published to mavenLocal; agent-brain green on it |
| B0 | bot | Kotlin skeleton (Gradle, detekt, CI job); contract-types module (bus schema, state-file formats with legacy adapters); golden replay recorder | >= 2 weeks of real state/ traces recorded; CI green; zero behavior change on the Python side |
| B1 | bot | Strangler begins with pure readers: zconn + spectator as a Kotlin daemon on the same files, shadow mode (Kotlin computes, Python still sends) | one week of byte-identical shadow decisions, then cutover with rollback |
| B2 | bot | Transport: Ktor client; same tg-offset / tg-cooldown / outbox / tg-last-ph semantics; breaker and retry via KAP; dry-run mode against live API reads | shadow-send dry-run, then cutover; 429 cooldown behavior verified against Telegram |
| B3 | bot | Turn execution: typed zcode_proto over bracket/awaiting; worker lifecycle under KAP Process; warm rotation preserved | a full phone turn executed by the Kotlin worker while Python owns commands |
| K1 | kap | The 5.0 slices the bot needs: KapStream first operators (buffer/conflate/latest) + Supervisor OneForOne | stream laws; SPEC's benchmark bar (< 2x Flow) |
| B4 | bot | The render core: live boxes, the blinking line, agent live view, on KapStream | "the live turn viewer powered by KapStream": the SPEC's own acceptance sentence, plus the incident-parity checklist |
| B5 | bot | Thin hybrid hooks (unix socket to the daemon); LaunchAgents to the single artifact; Python retirement | Python gone from deploy; runbook updated; heal layers mapped to Process supervision |

### 6.1 Invariants every phase preserves

These are the contract between processes and versions; each Kotlin phase
implements them exactly and the replay harness checks them:

- Atomic writes: tmp + fsync + rename (AtomicStore semantics,
  state/store.py).
- flock with a 10 s deadline on cross-process files (the F8
  self-deadlock lesson: sends never inside the lock).
- mtimes as shared clocks: tg-cooldown (only ever widened), hb-*,
  pid files, markers.
- Offset confirmed POST-dispatch (at-least-once, the 14/10 fix).
- One serial dispatcher with update dedup.
- Workers as OS processes separate from the poller (revisited
  deliberately in B3 when they become supervised children; that change
  gets its own ADR).
- Bilingual legacy reads (the persisted Spanish forms are permanent by
  design).
- The dumb outer ring: bot-healer stays dependency-free, no network.
- The app-server stdio protocol with `\u002e` framing, byte for byte.

## 7. What we deliberately do not do

- No big-bang rewrite. The strangler only.
- No new bot features during migration phases: armor parity first,
  features after.
- No Spring, no Akka, no Reactor, no STM, no DI framework (reasons in
  4.4).
- No protocol changes to the fork, the hooks, or the state files during
  B0..B4 (B5 revisits hooks with its own ADR).
- No KapStream designed by accident: surfaces discovered by the bot feed
  the SPEC; the SPEC ships laws; then the bot consumes. Order matters.

## 8. Appendix: the numbers behind this doc (verified 2026-10-28)

- bot @ b6eb407: src 64 .py / 17,625 LOC; tests 83 files / 19,770 LOC /
  1,012 `def test_` (the README's 985 has drifted); one runtime
  dependency (aiohttp 3.14.5); mypy report-only; deploy = gates + rsync +
  reload door.
- kap @ a976ad6 (4.1.0): modules kap-core / kap-resilience / kap-arrow /
  kap-kotest / kap-ktor / kap-ksp*; Kotlin 2.4.21, coroutines 1.11.0,
  Ktor 3.6.0; JDK 25 toolchain, bytecode 21; 1,096 @Test across 80
  suites; `Process` / `Supervisor` / `KapStream` exist only in SPEC-5.0
  (milestones 4.2 and 5.0).
- agent-brain @ a34aa3d: P0..P6 green (bus, hybrid RAG, memory with
  decay, Kafka bridge, brain-mcp, evals); the Kotlin side of the shared
  contracts lives there.
- The B4 risk core, by size: reasoning.py 1,931; agent_live.py 1,800;
  live_view.py 1,144; workers.py 1,567.
