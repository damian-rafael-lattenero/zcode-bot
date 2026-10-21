# ADR-004: stdlib-only, system Python 3.9

> **SUPERSEDED by ADR-010 (2026-10-15)**: Python 3.9 EOL, 3.13 runtime
> with a uv venv, aiohttp as the single runtime dependency, pytest
> dev-only. What remains in force from this ADR: the discipline of
> minimizing dependencies and the standard of justifying every exception
> in an ADR.


**Status:** accepted (2026-10-12, explicit request from the owner)

## Decision
Zero external dependencies. Runs on /usr/bin/python3 (CommandLineTools
3.9) under a LaunchAgent. Tests with unittest. The discipline a framework
would provide comes from named patterns + tests + ADRs.

## Rationale
- 24/7 daemon on the system python: `pip install` there breaks with every
  CLT update → operational risk with no rollback.
- The Telegram transport is 5 endpoints over urllib; bot frameworks impose
  event loops that clash with thread-per-worker + hook processes.

## Consequences
- Every pattern is hand-implemented but documented (Registry, Template
  Method, Repository, Strategy, Atomic Write, Command Registry).
- dataclasses yes (stdlib) when they help.
