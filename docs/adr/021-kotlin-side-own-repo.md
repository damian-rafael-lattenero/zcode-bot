# ADR-021: the Kotlin side lives in its own repo (zcode-bot-kotlin)

Date: 2026-10-29
Status: accepted

## Context

B0 of the KAP-first migration
([docs/kap-first-migration.md](../kap-first-migration.md)) calls for a Kotlin
skeleton, contract types for the persisted formats, and a golden replay
recorder. The first cut of the plan placed the Gradle project in a `kotlin/`
subtree of this repo.

## Decision

The Kotlin migration grows in a **separate repository**,
[zcode-bot-kotlin](https://github.com/damian-rafael-lattenero/zcode-bot-kotlin)
(Damian's call, mid-B0). This repo stays pure Python until B5 retires it.

## Consequences

- This repo's deploy (`deploy.sh` rsync of `src/`), CI and gates are untouched
  by the migration: zero risk of the strangler destabilizing the running bot.
- The **file contracts remain the only interface**: contract-types in the new
  repo decodes every format of `schemas.py` (bilingual legacy keys forever).
  A shape change in `schemas.py` is a change THERE too.
- The golden replay recorder (B0) is deployed additively: LaunchAgent
  `com.zcode.bot-recorder`, one fresh JVM per 60 s pass, traces under
  `~/.zcode/bot-recordings/` (never inside any repo; they contain real
  prompts). B1's entry gate is >= 2 weeks of recorded traces.
- KAP 4.2.0 (`Process`: restart policies, heartbeat, convergence laws) is
  already published; the B1 watchers will run on it.
