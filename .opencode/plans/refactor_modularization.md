> **SUPERSEDED (2026-10-15)**: este plan se ejecutó copiando en vez de moviendo y dejó HEAD sin importar (ver commits b7a3d75..57dc336, quemado en F0 1f1752a). El desmembramiento real va por delegación con suite verde por paso — estado en memoria del proyecto y README.

# Plan de Refactorización: Modularización de God Files

## Estado Actual

| Archivo | Líneas | Problema Principal |
|---------|--------|-------------------|
| `gateway.py` | 4445 | **God file** - 75 funciones, 0 clases, mezcla: config, HTTP, state, hooks, commands, workers, nuclear, live view, queues |
| `redis_streams.py` | 408 | 31 funciones sueltas, 2 clases - cohesión media |
| `http_client.py` | 329 | 1 función suelta, 2 clases - OK pero podría mejorar |
| `webhook_server.py` | 304 | 1 clase, 7 métodos - OK |
| `zcode_proto.py` | 408 | 1 clase, 5 funciones - OK |
| `concurrency.py` | 270 | 2 clases, 1 función - OK |
| `fswatch.py` | 142 | 6 funciones - OK |

## Objetivo: Arquitectura Modular por Dominio

```
src/zcode_bot/
├── __init__.py                 # exports públicos
├── config.py                   # config.py (extraído de gateway)
├── state.py                    # state.py (ya existe)
├── fswatch.py                  # fswatch (ya existe)
├── proto.py                    # proto.py (ya existe)
├── proto/
│   ├── __init__.py
│   ├── client.py               # ProtoClient
│   └── factory.py              # make(), client()
├── http/
│   ├── __init__.py
│   ├── client.py               # AsyncTelegramClient
│   ├── endpoints.py            # DEFAULT_ENDPOINTS, TelegramEndpointConfig
│   ├── circuit_breaker.py      # (ya existe)
│   └── rate_limiter.py         # (ya existe)
├── state/
│   ├── __init__.py
│   ├── store.py                # AtomicStore (ya existe)
│   ├── registry.py             # WorkersRegistry, QuestionFiles
│   └── manager.py              # _active_turn_*, _queue_*, _outbox_*
├── hooks/
│   ├── __init__.py
│   ├── handler.py              # run_hook, on_*_request
│   ├── permissions.py          # on_permission_request, _perm_*
│   ├── questions.py            # on_ask_user_question, _user_input_handler
│   └── lifecycle.py            # on_user_prompt, on_stop, on_session_start, on_tool_failure
├── commands/
│   ├── __init__.py
│   ├── registry.py             # CommandRegistry
│   ├── registry_builder.py     # builder de comandos
│   ├── core.py                 # cmd_start, cmd_help, cmd_ping, etc.
│   ├── tasks.py                # task_*, /task, /all_tasks, /run
│   ├── model.py                # model, thinking, mode, fork
│   ├── control.py              # stop, nuclear, deploy, status, quota
│   ├── queues.py               # queue, qgo, qdel, retry
│   ├── context.py              # ctx, compact, cot
│   └── webhook.py              # webhook commands
├── workers/
│   ├── __init__.py
│   ├── worker.py               # prompt_worker, _spawn_worker, cmd_run_turn
│   ├── live_view.py            # _live_view, _cot_*, _tool_*, trace
│   └── nuclear.py              # nuclear, app_restart_cycle
├── webhook/
│   ├── __init__.py
│   ├── server.py               # WebhookServer
│   └── handlers.py             # handle_update, process_update
├── utils/
│   ├── __init__.py
│   ├── config.py               # load_config, save_config, DEFAULTS
│   ├── logging.py              # log, pwlog, log_payload
│   ├── formatting.py           # esc, preview, fmt_tok, fmt_num, etc.
│   ├── http.py                 # tg_api, send, send_document
│   ├── datetime.py             # datetime_dt, _uptime_txt
│   └── validation.py           # classify, first, preview, esc
├── reports/
│   ├── __init__.py
│   ├── builder.py              # build_list_report
│   ├── tasks.py                # task_list_report, compact_list_report
│   ├── quota.py                # quota_report, usage_report
│   └── context.py              # context_report
└── gateway.py                  # facade delegador (entry point CLI/hooks)
```

## Fases de Ejecución

### Fase 1: Extracción de Utilidades Comunes (Base)
- `config.py` - DEFAULTS, load_config, save_config
- `utils/formatting.py` - esc, preview, fmt_tok, fmt_num, datetime_dt, etc.
- `utils/validation.py` - classify, first, preview, classify
- `utils/http.py` - tg_api, send, send_document
- `utils/logging.py` - log, pwlog, log_payload

### Fase 2: Configuración y Estado
- `config.py` - load_config, save_config, DEFAULTS, configured
- `state/store.py` - AtomicStore (ya existe, mover)
- `state/registry.py` - WorkersRegistry, QuestionFiles (de concurrency)
- `state/manager.py` - _active_turn_*, _queue_*, _outbox_*, _stop_*

### Fase 3: Proto & HTTP
- `proto/client.py` - ProtoClient
- `proto/factory.py` - make(), client()
- `http/client.py` - AsyncTelegramClient
- `http/endpoints.py` - DEFAULT_ENDPOINTS, TelegramEndpointConfig
- `http/circuit_breaker.py` (existente)
- `http/rate_limiter.py` (existente)

### Fase 4: Hooks (Eventos)
- `hooks/handler.py` - run_hook
- `hooks/permissions.py` - on_permission_request, _perm_*
- `hooks/questions.py` - on_ask_user_question, _user_input_handler
- `hooks/lifecycle.py` - on_user_prompt, on_stop, on_session_start, on_tool_failure

### Fase 5: Workers & Live View
- `workers/worker.py` - prompt_worker, _spawn_worker, cmd_run_turn
- `workers/live_view.py` - _live_view, _cot_*, _tool_*, build_trace, _send_long_reply
- `workers/nuclear.py` - nuclear, app_restart_cycle, _nuc_*

### Fase 6: Comandos (Command Pattern)
- `commands/registry.py` - CommandRegistry
- `commands/core.py` - cmd_start, cmd_help, cmd_ping, etc.
- `commands/tasks.py` - task_*, /task, /all_tasks, /run
- `commands/model.py` - model, thinking, mode, fork
- `commands/control.py` - stop, nuclear, deploy, status, quota
- `commands/queues.py` - queue, qgo, qdel, retry
- `commands/context.py` - ctx, compact, cot
- `commands/webhook.py` - webhook commands

### Fase 5: Workers & Live View
- `workers/worker.py` - prompt_worker, _spawn_worker, cmd_run_turn
- `workers/live_view.py` - _live_view, _cot_*, _tool_*, build_trace, _send_long_reply
- `workers/nuclear.py` - nuclear, app_restart_cycle, _nuc_*

### Fase 6: Comandos (Command Pattern)
- `commands/registry.py` - CommandRegistry
- `commands/core.py` - cmd_start, cmd_help, cmd_ping, etc.
- `commands/tasks.py` - task_*, /task, /all_tasks, /run
- `commands/model.py` - model, thinking, mode, fork
- `commands/control.py` - stop, nuclear, deploy, status, quota
- `commands/queues.py` - queue, qgo, qdel, retry
- `commands/context.py` - ctx, compact, cot
- `commands/webhook.py` - webhook commands

### Fase 7: Webhook Server
- `webhook/server.py` - WebhookServer
- `webhook/handlers.py` - handle_update, process_update

### Fase 6: Reports & Context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Fase 6: Reports & Context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Fase 7: Webhook Server
- `webhook/server.py` - WebhookServer
- `webhook/handlers.py` - handle_update, process_update

### Fase 7: Reports & Context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Fase 8: Webhook Server
- `webhook/server.py` - WebhookServer
- `webhook/handlers.py` - handle_update, process_update

### Fase 8: Reports & Context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Fase 9: Gateway Facade (Entry Point)
- `gateway.py` - ~200 líneas, solo delega a módulos
- `main()` - entry point CLI/hooks

### Fase 9: Cleanup & Tests
- Mover tests a estructura paralela
- Actualizar imports en tests
- Verificar 254 tests pasan
- Ejecutar deploy.sh

## Criterios de Éxito

| Métrica | Objetivo |
|---------|----------|
| gateway.py | < 300 líneas (facade) |
| Módulos | < 300 líneas c/u |
| Acoplamiento | Bajo (inyección de dependencias) |
| Tests | 254/254 pasan |
| Imports | Explícitos, sin ciclos |

## Riesgos y Mitigación

| Riesgo | Mitigación |
|--------|------------|
| Tests rotos | Ejecutar tests tras CADA fase |
| Imports circulares | Reglas estrictas: no imports hacia arriba |
| Rotura de CLI | Mantener gateway.py como facade delegador |
| Tests lentos | Ejecutar en paralelo (-n auto) |

## Orden de Ejecución Recomendado

1. **Semana 1**: Fases 1-2 (Base + Config + Estado)
2. **Semana 2**: Fases 3-4 (Proto/HTTP + Hooks)
3. **Semana 3**: Fases 4-5 (Workers + Live View)
4. **Semana 4**: Fases 6-7 (Comandos + Webhook)
5. **Semana 5**: Fases 8-10 (Reports, Gateway facade, Tests, Deploy)
