# ADR-010 — Runtime Python 3.13 (uv standalone) + dependencias: aiohttp único, pytest dev

**Estado**: aceptado (2026-10-15). **Supersede ADR-004** (stdlib-only
Python 3.9): el 3.9 del sistema llegó EOL en oct-2025 y aiohttp ≥3.10 ya
no lo soporta oficialmente — la "excepción silenciosa" de
`requirements.txt` (commits wip del 14/10) queda formalizada acá.

## Decisión

1. **Runtime**: CPython **3.13** — build standalone arm64 gestionado por
   **uv** (`~/.local/share/uv/python/`), en un venv propio del bot
   (`~/.zcode/hooks/venv`). Ni el Python del sistema ni Homebrew-python
   (existe para fórmulas de Homebrew y rompe en upgrades). `bootstrap.sh`
   reproduce el runtime entero de cero, idempotente.
2. **Dependencias de runtime: UNA** — `aiohttp==3.14.5` (pinned exacto en
   requirements.txt): cliente HTTP async (AsyncTelegramClient: rate
   limiter + circuit breaker por endpoint) y server web (webhook receiver,
   ADR-011) en el mismo paquete, nativo asyncio, aceleración C. Elegido
   sobre httpx (sin server) y FastAPI/uvicorn (dos deps + pydantic para
   un endpoint). Benchmarks 2026: el más rápido en async puro.
3. **Dependencias de desarrollo** (requirements-dev.txt, pinned):
   pytest, pytest-xdist (suite en `-n auto`, 16 workers: ~6 s), pytest-
   timeout (60 s por test — un test que cuelga no puede colgar la suite;
   lección del dispatcher con drain sobre consumidor muerto).
4. **NO** a frameworks de bot (aiogram/python-telegram-bot: el valor es
   routing/FSM/multi-usuario, este bot es de dueño único con semántica
   endurecida por incidentes; además ya abandonaron 3.9) y **NO** a
   brokers/mensajería (el estado por archivos + flock + kqueue ES el
   modelo de actores del bot — ADR-003/F16; redis fue borrado en F0).
5. **deploy.sh** usa el venv automáticamente si existe (fallback: python3
   del sistema); los LaunchAgents apuntan al venv (runbook: recarga de
   plists).

## Consecuencias

- Python 3.13 soportado hasta 2029; walrus, dataclasses y typing moderno
  disponibles sin hacks de compatibilidad.
- Todo cambio de dependencia pasa por este ADR (o uno nuevo) — nunca otra
  "excepción silenciosa" en requirements.txt.
- La suite corre con el MISMO intérprete que producción (venv) — se
  terminó la divergencia intérprete-tests-prod.
