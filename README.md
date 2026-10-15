<!-- test e2e F7: edit aleatorio de prueba (se revierte en el próximo commit) -->

# bot — Gateway Telegram ↔ ZCode

Control total de ZCode desde el celular (headless: pantalla bloqueada OK).
Un bot (@glm_lattenero_2_bot) para conversar, preguntar, compactar, frenar
y monitorear. Runtime **Python 3.13** (venv uv) + **aiohttp** (única dep
de runtime). 319 tests, suite paralela en ~6 s.

## Arquitectura (post-refactor producción, oct 2026)

```
[Telegram (celular)]
      ⇅ polling (long-poll 3s)  —o—  webhook (HTTPS + secret, ADR-011)
[Proceso gateway — LaunchAgent com.zcode.telegram-cmds]
  └─ asyncio: runtime_async() — UN loop para transporte + supervisor
       ├─ transportes excluyentes: poller.AsyncPoller | webhook_server
       ├─ dispatcher.UpdateDispatcher — serial, dedup update_id, owner
       ├─ gateway.process_update_sync — el pipeline por-update (seam)
       ├─ commands/ — CommandRegistry + dispatch (~25 handlers, F4i)
       ├─ workers-PROCESO (--run-turn, F16): dueños de su runtime
       ├─ core — el seam compartido (sesión, registros, stop, permisos)
       ├─ live_view.LiveTurnView — la caja viva del turno (F4d)
       ├─ reporting / messages / nuclear / hooks — por dominio (F4)
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
El gateway es facade puro (composition root, 3 definiciones propias —
ADR-012: el desmembramiento por delegación, mover sin copiar).

## Layout

```
src/zcode-telegram.py    shim (entrypoint del LaunchAgent/hooks/subprocesos)
src/zcode_bot/           el paquete:
  gateway.py             facade: process_update_sync + runtime_async + main
  core.py                seam compartido (sesión, _workers()/_questions(),
                         stop-markers, perm-cache, lecturas de cola/turnos)
  messages.py            parsers de ProtoClient (entidad: ToolStep)
  live_view.py           LiveTurnView + guardas anti-OOM + _turn_end_poll
                         (F7 live action: tools 🔵→✅ en vivo, afterSeq)
  nuclear.py             ☢️ Nuclear 2.0 (entidad: NuclearReport)
  reporting.py           informes y entregas (entidad: Task)
  workers.py             cola + spawn + prompt_worker (QueueItem, WorkerEntry)
  updates.py             plumbing Telegram (offset, busy, _cb_text, polling)
  hooks.py               entrypoints on_* del CLI (wait_reply via commands)
  commands/              registry.py + dispatch.py (CommandRegistry, F4i)
  dispatcher.py/poller.py/webhook_server.py   núcleo asyncio (F2/F3)
  state/ telegram/ utils/          infra testeada (F1/F2)
  models.py config.py    catálogo único / paths únicos
src/zcode_proto.py       cliente stdio del protocolo
tests/                   pytest (unittest-style): golden UX, dispatcher, poller,
                         webhook, LiveTurnView, prompt_worker — 16 workers
scripts/check_no_duplicates.py   gate: un solo cuerpo vivo por concepto
deploy.sh                tests → gate → backup → copia → ritual → puerta
bootstrap.sh             runtime desde cero (uv + 3.13 + venv + deps)
requirements(.dev).txt   deps pinneadas (runtime: aiohttp; dev: pytest+xdist+timeout)
docs/adr/                13 ADRs (004, 001 y plan nemotron con supersede)
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

## El turno en vivo (F7 — live action)

Mientras un turno corre, el chat muestra TODO lo que hace el agente:

- **Caja viva**: el CoT actual (pensando / escribiendo) y, al lado, la tool
  corriente — el comando se ve **tipeándose** mientras el modelo lo genera
  (`input_streaming`) y después `🔵 corriendo`.
- **Caja por tool**: apenas el input completa ya viaja al chat una caja
  con el comando completo / diff real (`_tool_caja`); nace `🔵` y se
  **edita** a `✅`/`❌` con la salida cuando llega el resultado.
- **Cajas de CoT**: el razonamiento completo queda scrolleable en mensajes
  permanentes «CoT N» mientras corre.

Canillas: `cot_stream` / `tools_stream` (default ON), `live_progress`.
Infra: poll de eventos **incremental** (`afterSeq`, cursor inicial del
`eventSeq` del subscribe — el replay completo infló un turno real a 3.1GB
RSS); guardas anti-OOM como red de seguridad; emparejamiento
stream↔historial por `callID`/`toolCallId`. Detalles: ADR-013.

## Reglas de la casa

- **Un solo cuerpo vivo por concepto** — lo enforcea el gate anti-dup en
  CI (la lección del refactor del 14/10 que dejó HEAD sin importar).
- **Todo por atributo, nada horneado al importar**: `config.STATE_DIR`,
  `_workers()`, `uhttp.send` (ADR-012). El seam de parcheo de los tests
  es SIEMPRE el módulo dueño: `mock.patch.object(zc_http, "send")` — un
  rebind en el namespace equivocado queda mudo y el test deja de probar.
- **Nada de sleeps para coordinar** (ADR-009): esperas reactivas con TTL;
  pytest-timeout 60 s por test.
- Mover, no copiar: cada extracción con suite verde en el mismo commit;
  movimiento y cambio de representación (dataclasses) en commits
  separados, con round-trip de los JSON de state.

## Runbook

Ver `docs/runbook.md` (runtime, transporte webhook/polling, incidentes).
