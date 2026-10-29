# ADR-023: B1a — the strangler begins with zconn, in shadow

Date: 2026-10-29
Status: deployed (shadow); supersedes nothing; companion of
`docs/kap-first-migration.md` §6 (B1) and
zcode-bot-kotlin `docs/parity-and-engineering.md` §6/§7.

## Context

B1 starts the strangler migration with the pure readers: zconn and
spectator watching the same files the Python side watches, computing the
same decisions, WITHOUT sending — shadow mode. zconn goes first (B1a):
it is small, pure, and incident-hardened (F85/F85c — the first-reason
rule, the sputter rule, the since-creep deafness), which makes it the
ideal first proof of the whole pipeline: KAP `Process` in production,
the CLI-log contract, the E1 logging stack, the parity artifacts.

kap 4.2.0 (`Process`) is on Maven Central since 2026-10-29, so the
daemon consumes the real library, not mavenLocal.

## Decision

A long-lived Kotlin daemon (`zcode-bot-kotlin` `modules/daemon`,
LaunchAgent `com.zcode.bot-daemon`, KeepAlive) runs one KAP `Process`
per watcher. Today: `zconn-shadow` — `Kap { one pass }.repeat(spaced(5s))`
wrapped in `Process` (exponential restarts, heartbeat every 30 s, the
beat independent of cycle progress). launchd is the dumb outer ring; a
restart is a bug report, not a strategy.

**Shadow contract (additive only — zero Python changes):**

- READS: `~/.zcode/cli/log/zcode-*.jsonl` (shared with the Python
  poller's `zconn.listen`; read-only).
- WRITES: only `~/.zcode/bot-kotlin/` — `state/zconn.json` (same shape
  as the Python side's, in the daemon's OWN dir), `shadow/zconn.jsonl`
  (one JSON line per decision: ts/ts_iso/watcher/decision/text/event_ts,
  the text byte-identical to what Python's `_notify` would send),
  `logs/daemon.jsonl` (E1: JSONL+MDC, async neverBlock), and
  `state/hb-zconn-shadow` (mtime heartbeat).
- NEVER sends. The Python bot keeps speaking.

**The port keeps Python's logic exactly** (episode machine, sputter
window 120 s, first-reason-wins, `since` advanced only over real event
timestamps clamped to now, arming at NOW on first run) with its
incident suite ported case by case plus property laws (episode
anchoring, sputter silence, ordering). Two warts are mirrored
DELIBERATELY because diverging would fail the parity gate:

1. `iter_events(last_files=1)`: after the midnight rollover the
   previous file's unwritten tail is dropped (the daemon reads only the
   newest log).
2. The 1 s since-tolerance re-admits the last event after a restart;
   the machine's closed track makes that replay silent (a clock far
   behind the events would re-announce — Python behaves the same).

Two things are BETTER (decision-neutral, verified by tests): an
incremental cursor instead of re-scanning the whole file every cycle,
and atomic (tmp+fsync+rename) state writes that only happen when there
were events (mtime stays a clock).

Known cosmetic divergence: Kotlin serializes large doubles in
scientific notation (`1.79E9`). Valid JSON, `json.load`-friendly,
round-trips; a plain-notation serializer is queued if a byte-diff of
state files ever matters. Decisions and their texts are unaffected.

## The parity gate and the cutover

The exit criterion is one week of byte-identical shadow decisions: real
cuts land in `shadow/zconn.jsonl` while Python sends its notices
(`zconn down/up` in the kb log + Telegram); the comparator (B1c) diffs
both streams. Cutover = a knob silences Python's zconn and the daemon
starts sending through B2's transport; rollback = the knob back. Until
then the daemon is a proof, not a dependency.

## Verification (2026-10-29, live)

Startup line in `daemon.jsonl` (JSON, MDC component=zconn), heartbeat
touching every 30 s, state armed at NOW, and — the interesting one — a
restart of the daemon re-read the day's whole log and announced
nothing (the since floor + closed track held against real data).

Suite: zcode-bot-kotlin tests green with detekt ACTUALLY running
(the `~/.gradle/gradle.properties` JVM pin had been silently skipping
local detekt — the deploy scripts now override
`org.gradle.java.home` on the command line; CI was never affected).
