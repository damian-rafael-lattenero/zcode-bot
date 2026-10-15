# bot — Gateway Telegram ↔ ZCode

Control total de ZCode desde el celular (headless: pantalla bloqueada OK).
Un bot (@glm_lattenero_2_bot) para conversar, preguntar, compactar, frenar
y monitorear. Runtime **Python 3.13** (venv uv) + **aiohttp** (única dep
de runtime). 288 tests, suite paralela en ~6 s.

## Arquitectura (post-refactor producción, oct 2026)

```
[Telegram (celular)]
      ⇅ polling (long-poll 3s)  —o—  webhook (HTTPS + secret, ADR-011)
[Proceso gateway — LaunchAgent com.zcode.telegram-cmds]
  └─ asyncio: runtime_async() — UN loop para transporte + supervisor
       ├─ transportes excluyentes: poller.AsyncPoller | webhook_server
       ├─ dispatcher.UpdateDispatcher — serial, dedup update_id, owner
       ├─ gateway.process_update_sync — el pipeline por-update (seam)
       ├─ commands.CommandRegistry — comandos por tabla
       ├─ workers-PROCESO (--run-turn, F16): dueños de su runtime
       ├─ state.AtomicStore + registries file-backed (flock, kqueue)
       ├─ proto.make — clientes del protocolo (stderr por PID)
       ├─ telegram.AsyncTelegramClient — rate limiter + circuit breaker
       └─ utils/ — config (paths por atributo), send/outbox, formatting
[Hooks — procesos por evento (▶️ ✅ ❓ 🔐)]  → mismo paquete, modo hook
[Runtimes ZCode — app-server --stdio (JSON-RPC)]  ← ADR-002/003
```

Invariante central: **UN dispatcher procesa updates** (ADR-011, sucede a
ADR-001). Dos escritores (app ↔ bot): ADR-003. /stop solo-corridas-del-
celular: ADR-002. Transportes polling/webhook mutuamente excluyentes.

## Layout

```
src/zcode-telegram.py    shim (entrypoint del LaunchAgent/hooks/subprocesos)
src/zcode_bot/           el paquete (gateway + dispatcher/poller/webhook/state/utils/telegram)
src/zcode_proto.py       cliente stdio del protocolo
tests/                   pytest (unittest-style): golden UX, dispatcher, poller,
                         webhook, concurrency — 16 workers, fixture ~/.zcode fake
scripts/check_no_duplicates.py   gate: un solo cuerpo vivo por concepto
deploy.sh                tests → gate → backup → copia → ritual → puerta
bootstrap.sh             runtime desde cero (uv + 3.13 + venv + deps)
requirements(.dev).txt   deps pinneadas (runtime: aiohttp; dev: pytest+xdist+timeout)
docs/adr/                11 ADRs (004 y 001 con supersede marcado)
docs/runbook.md          operación, runtime, transporte, incidentes
```

## Uso diario

```bash
./bootstrap.sh                       # preparar/reparar el runtime (idempotente)
./deploy.sh                          # deploy completo (tests paralelos + gate de import)
./deploy.sh --rollback               # restaurar la versión anterior
~/.zcode/hooks/venv/bin/python3 -m pytest -n auto -q   # suite (~6 s)
alias py3="$HOME/.zcode/hooks/venv/bin/python3"        # atajo (en ~/.zshrc)
```

Comandos del bot: `/help` en el chat (menú con scope del dueño).

## Reglas de la casa

- **Un solo cuerpo vivo por concepto** — lo enforcea el gate anti-dup en
  CI (la lección del refactor del 14/10 que dejó HEAD sin importar).
- **Paths por atributo**: `config.STATE_DIR`, nunca `from config import
  STATE_DIR` (la copia diverge del fixture en tests).
- **Nada de sleeps para coordinar** (ADR-009): esperas reactivas con TTL;
  pytest-timeout 60 s por test.
- Mover, no copiar: cada extracción con suite verde en el mismo commit.

## Runbook

Ver `docs/runbook.md` (runtime, transporte webhook/polling, incidentes).
