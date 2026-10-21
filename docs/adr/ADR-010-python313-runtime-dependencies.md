# ADR-010 — Python 3.13 runtime (uv standalone) + dependencies: aiohttp only, pytest dev

**Status:** accepted (2026-10-15). **Supersedes ADR-004** (stdlib-only
Python 3.9): the system 3.9 reached EOL in oct-2025 and aiohttp ≥3.10 no
longer officially supports it — the "silent exception" in
`requirements.txt` (wip commits of 10/14) is formalized here.

## Decision

1. **Runtime**: CPython **3.13** — standalone arm64 build managed by
   **uv** (`~/.local/share/uv/python/`), in the bot's own venv
   (`~/.zcode/hooks/venv`). Neither the system Python nor Homebrew-python
   (it exists for Homebrew formulas and breaks on upgrades). `bootstrap.sh`
   reproduces the whole runtime from scratch, idempotently.
2. **Runtime dependencies: ONE** — `aiohttp==3.14.5` (exact pin in
   requirements.txt): async HTTP client (AsyncTelegramClient: rate
   limiter + circuit breaker per endpoint) and web server (webhook
   receiver, ADR-011) in the same package, native asyncio, C
   acceleration. Chosen over httpx (no server) and FastAPI/uvicorn (two
   deps + pydantic for one endpoint). 2026 benchmarks: the fastest in
   pure async.
3. **Development dependencies** (requirements-dev.txt, pinned): pytest,
   pytest-xdist (suite in `-n auto`, 16 workers: ~6 s), pytest-timeout
   (60 s per test — a hanging test cannot hang the suite; lesson from
   the dispatcher with drain over a dead consumer).
4. **NO** bot frameworks (aiogram/python-telegram-bot: the value is
   routing/FSM/multi-user, this bot is single-owner with semantics
   hardened by incidents; besides, they had already dropped 3.9) and
   **NO** brokers/messaging (file-based state + flock + kqueue IS the
   bot's actor model — ADR-003/F16; redis was deleted in F0).
5. **deploy.sh** uses the venv automatically if it exists (fallback:
   system python3); the LaunchAgents point at the venv (runbook: plist
   reloads).

## Consequences

- Python 3.13 supported until 2029; walrus, dataclasses and modern
  typing available without compat hacks.
- Every dependency change goes through this ADR (or a new one) — never
  again a "silent exception" in requirements.txt.
- The suite runs with the SAME interpreter as production (venv) — the
  tests/prod interpreter divergence is over.
