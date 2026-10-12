# bot — Gateway Telegram ↔ ZCode

Control total de ZCode desde el celular (headless: pantalla bloqueada OK).
Un bot (@glm_lattenero_2_bot) para conversar, preguntar, compactar, frenar y
monitorear. **Cero dependencias** (Python 3.9 stdlib). 54 tests automáticos.

## Arquitectura

```
[Telegram (celular)]
      ⇅ getUpdates/sendMessage (único lector: ADR-001)
[Poller — LaunchAgent com.zcode.telegram-cmds]
  └─ src/zcode_bot/gateway.py (shim: zcode-telegram.py, 17 líneas)
       ├─ commands.CommandRegistry — 19 comandos (tabla)
       ├─ reports.build_list_report — listas tocables (Template Method)
       ├─ concurrency.WorkersRegistry / QuestionsBroker (lock total)
       ├─ state.AtomicStore — estado persistente atómico (+flock outbox)
       ├─ proto.make — factory de clientes del protocolo (stderr por PID)
       ├─ cli_logs.iter_events — scanner de logs (Strategy)
       ├─ tasks_repo — sqlite read-only (Repository)
       └─ workers.prompt_worker — turnos headless + streaming en vivo
[Hooks — procesos por evento (▶️ ✅ ❓ 🔐)]  → mismo paquete, modo `hook`
[Runtimes ZCode — app-server --stdio (JSON-RPC)]  ← ADR-002/003
```

Dos escritores (app ↔ bot): ver ADR-003. /stop solo-corridas-del-celular:
ADR-002. Hechos verificados contra doc oficial: `docs/REFERENCES.md`.

## Layout

```
src/zcode-telegram.py    shim (entrypoint del LaunchAgent/hooks/subprocesos)
src/zcode_bot/           el paquete (gateway + 7 módulos)
src/zcode_proto.py       cliente stdio del protocolo
tests/                   unittest: golden UX + pure + state + concurrency
                         + repo + F3/F4 (54 tests, fixture ~/.zcode fake)
deploy.sh                tests → backup → copia → ritual → puerta → verify
docs/REFERENCES.md       hechos verificados con URL y fecha
docs/adr/                5 ADRs
```

## Uso diario

```bash
./deploy.sh              # deploy completo (espera corridas del bot solo)
./deploy.sh --rollback   # restaurar la versión anterior
python3 -m unittest discover -s tests   # suite completa (~5 s)
```

Comandos del bot: `/help` en el chat (menú con scope del dueño).

## Runbook

Ver `docs/runbook.md` (incidentes conocidos + recuperación).
