# ADR-009 — Reactive suite: asyncio tests without sleeps, production with reactive waits and TTL

**Status:** accepted (2026-10-15)
**Context:** the suite coordinated threads with `sleep(0.3)`/`sleep(3.5)`
and the production code had imperative waits (polling with a fixed sleep
in `ProtoClient.request`, a setup race in `fswatch`, `while True` with
`sleep` in the reader and in `_wait_cli_event`). The tests were slow and
flaky: the sleep had to be "long enough" for the day's hardware. Damian
set the doctrine: *everything reactive, no sleeps, waits with TTL — "like
a monad: the whole test flow composes"*.

## Decision

**Tests**: the whole suite is `unittest.IsolatedAsyncioTestCase` (stdlib
3.9 then; 3.13 today) with the kit `tests/asynkit.py` — one primitive
per responsibility:

- `call(fn, ...)` — blocking SUT call → `asyncio.to_thread`; awaited
  with `await`.
- `wait_until(pred, timeout, msg)` — rendezvous by condition: spins with
  `sleep(0)` (yield to the loop, NOT a timed wait). Determinism: the
  predicate runs first and ANY producer/consumer order converges without
  temporal coordination; TTL expired → AssertionError.
- `Sig` — thread→loop bridge (`call_soon_threadsafe`).
- `Chan` — bounded producer→consumer channel (`asyncio.Queue` with
  `join()`/`task_done()` for pools); tests scale linearly by adding
  coroutines to the `gather` (N producers × M resolvers, test_concurrency),
  without touching the kit.

**Production** (still threads + kqueue + files per ADR-008; what changed
is HOW it waits):

1. `ProtoClient.request` waits on a `threading.Condition` (woken by
   `notify_all` in `_dispatch`; server death → `dead` flag + notify).
   The reader is a pure generator (`_frames`) over `select.select` with
   a 0.25 s slice — the data flow is declarative.
2. `fswatch` closes the setup race: baseline snapshot BEFORE arming the
   kqueue + a safety scan on every empty slice. And the canonical
   predicate-wait primitive is `fswatch.wait_for(pred, paths, timeout)`:
   it checks the predicate first and blocks in the kernel until event or
   TTL.
3. `_wait_cli_event` (gateway then; core.py today) uses
   `fswatch.wait_for`; `cmd_register` uses long-poll as its reactive
   wait.

**Residual LEGITIMATE sleeps** (not busy-waiting; each is a policy, not
a coordination):

| Category | Examples |
|---|---|
| Retry with backoff | send 0/1/3 s, 5 s on 429 |
| Telegram anti-flood pacing | ~1 msg/s CoT (1.2 s), edits ≥3 s |
| Post-event settle time | nuclear `sleep(2)` pre-kickstart (`ctx_probe` 1.5 s, retired in F31) |
| TTL with injectable wait | `_nuc_wait` (the nuclear path; the 100% reactive version would be kqueue `EVFILT_PROC` — unfavorable risk/benefit on the emergency path) |
| REPORTING monitor cadence | `cmd_stuck_watch`, `cmd_watch_app` |
| No-kqueue fallback | `fswatch._wait_poll` |

## Consequences

- Deterministic suite: zero coordination flakiness; times appear only as
  upper-bound assertions (<1.5 s, <2 s).
- Test ACID: Atomic (fresh fixture per test, `support.fresh`),
  Consistent (no wall-clock in assertions), Isolated (new loop per test
  + tempdirs + QUESTIONS/WORKERS rebind in support), Durable (asserts on
  committed files).
- Anti-regression rule: a new `time.sleep` in tests/ is a bug; in src/
  it must fall into one of the categories above (or a new one with an
  ADR).
- Documented pending: `EVFILT_PROC` for `_nuc_wait` (to be re-evaluated
  if the nuclear ever needs sub-second precision again).
