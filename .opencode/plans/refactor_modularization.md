> **SUPERSEDED (2026-10-15)**: this plan was executed by copying instead of
> moving and left HEAD unimportable (see commits b7a3d75..57dc336, burned
> in F0 1f1752a). The real dismemberment goes by delegation with a green
> suite at every step — state tracked in the project memory and README.
> Kept as a HISTORICAL record of the decisions taken; it does not
> describe the current layout of src/.

# Refactoring Plan: Modularization of the God Files

## State at the time

| File | Lines | Main problem |
|------|-------|--------------|
| `gateway.py` | 4445 | **God file** - 75 functions, 0 classes, mixes: config, HTTP, state, hooks, commands, workers, nuclear, live view, queues |
| `redis_streams.py` | 408 | 31 loose functions, 2 classes - medium cohesion |
| `http_client.py` | 329 | 1 loose function, 2 classes - OK but could improve |
| `webhook_server.py` | 304 | 1 class, 7 methods - OK |
| `zcode_proto.py` | 408 | 1 class, 5 functions - OK |
| `concurrency.py` | 270 | 2 classes, 1 function - OK |
| `fswatch.py` | 142 | 6 functions - OK |

## Goal: modular architecture by domain

```
src/zcode_bot/
├── __init__.py                 # public exports
├── config.py                   # config.py (extracted from gateway)
├── state.py                    # state.py (already exists)
├── fswatch.py                  # fswatch (already exists)
├── proto.py                    # proto.py (already exists)
├── proto/
│   ├── __init__.py
│   ├── client.py               # ProtoClient
│   └── factory.py              # make(), client()
├── http/
│   ├── __init__.py
│   ├── client.py               # AsyncTelegramClient
│   ├── endpoints.py            # DEFAULT_ENDPOINTS, TelegramEndpointConfig
│   ├── circuit_breaker.py      # (already exists)
│   └── rate_limiter.py         # (already exists)
├── state/
│   ├── __init__.py
│   ├── store.py                # AtomicStore (already exists)
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
│   ├── registry_builder.py     # command builder
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
└── gateway.py                  # delegating facade (CLI/hooks entry point)
```

## Execution phases

### Phase 1: Common utilities extraction (base)
- `config.py` - DEFAULTS, load_config, save_config
- `utils/formatting.py` - esc, preview, fmt_tok, fmt_num, datetime_dt, etc.
- `utils/validation.py` - classify, first, preview, classify
- `utils/http.py` - tg_api, send, send_document
- `utils/logging.py` - log, pwlog, log_payload

### Phase 2: Configuration and state
- `config.py` - load_config, save_config, DEFAULTS, configured
- `state/store.py` - AtomicStore (already exists, move it)
- `state/registry.py` - WorkersRegistry, QuestionFiles (from concurrency)
- `state/manager.py` - _active_turn_*, _queue_*, _outbox_*, _stop_*

### Phase 3: Proto & HTTP
- `proto/client.py` - ProtoClient
- `proto/factory.py` - make(), client()
- `http/client.py` - AsyncTelegramClient
- `http/endpoints.py` - DEFAULT_ENDPOINTS, TelegramEndpointConfig
- `http/circuit_breaker.py` (existing)
- `http/rate_limiter.py` (existing)

### Phase 4: Hooks (events)
- `hooks/handler.py` - run_hook
- `hooks/permissions.py` - on_permission_request, _perm_*
- `hooks/questions.py` - on_ask_user_question, _user_input_handler
- `hooks/lifecycle.py` - on_user_prompt, on_stop, on_session_start, on_tool_failure

### Phase 5: Workers & live view
- `workers/worker.py` - prompt_worker, _spawn_worker, cmd_run_turn
- `workers/live_view.py` - _live_view, _cot_*, _tool_*, build_trace, _send_long_reply
- `workers/nuclear.py` - nuclear, app_restart_cycle, _nuc_*

### Phase 6: Commands (Command pattern)
- `commands/registry.py` - CommandRegistry
- `commands/core.py` - cmd_start, cmd_help, cmd_ping, etc.
- `commands/tasks.py` - task_*, /task, /all_tasks, /run
- `commands/model.py` - model, thinking, mode, fork
- `commands/control.py` - stop, nuclear, deploy, status, quota
- `commands/queues.py` - queue, qgo, qdel, retry
- `commands/context.py` - ctx, compact, cot
- `commands/webhook.py` - webhook commands

### Phase 5: Workers & live view
- `workers/worker.py` - prompt_worker, _spawn_worker, cmd_run_turn
- `workers/live_view.py` - _live_view, _cot_*, _tool_*, build_trace, _send_long_reply
- `workers/nuclear.py` - nuclear, app_restart_cycle, _nuc_*

### Phase 6: Commands (Command pattern)
- `commands/registry.py` - CommandRegistry
- `commands/core.py` - cmd_start, cmd_help, cmd_ping, etc.
- `commands/tasks.py` - task_*, /task, /all_tasks, /run
- `commands/model.py` - model, thinking, mode, fork
- `commands/control.py` - stop, nuclear, deploy, status, quota
- `commands/queues.py` - queue, qgo, qdel, retry
- `commands/context.py` - ctx, compact, cot
- `commands/webhook.py` - webhook commands

### Phase 7: Webhook server
- `webhook/server.py` - WebhookServer
- `webhook/handlers.py` - handle_update, process_update

### Phase 6: Reports & context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Phase 6: Reports & context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Phase 7: Webhook server
- `webhook/server.py` - WebhookServer
- `webhook/handlers.py` - handle_update, process_update

### Phase 7: Reports & context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Phase 8: Webhook server
- `webhook/server.py` - WebhookServer
- `webhook/handlers.py` - handle_update, process_update

### Phase 8: Reports & context
- `reports/builder.py` - build_list_report
- `reports/tasks.py` - task_list_report, compact_list_report
- `reports/quota.py` - quota_report, usage_report, quota_live, read_quota_cache
- `reports/context.py` - context_report, ctx_tasks, coding_usage

### Phase 9: Gateway facade (entry point)
- `gateway.py` - ~200 lines, only delegates to modules
- `main()` - CLI/hooks entry point

### Phase 9: Cleanup & tests
- Move tests to a parallel structure
- Update imports in tests
- Verify the 254 tests pass
- Run deploy.sh

## Success criteria

| Metric | Goal |
|--------|------|
| gateway.py | < 300 lines (facade) |
| Modules | < 300 lines each |
| Coupling | Low (dependency injection) |
| Tests | 254/254 pass |
| Imports | Explicit, no cycles |

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Broken tests | Run tests after EVERY phase |
| Circular imports | Strict rules: no upward imports |
| CLI breakage | Keep gateway.py as a delegating facade |
| Slow tests | Run in parallel (-n auto) |

## Recommended execution order

1. **Week 1**: Phases 1-2 (base + config + state)
2. **Week 2**: Phases 3-4 (Proto/HTTP + hooks)
3. **Week 3**: Phases 4-5 (workers + live view)
4. **Week 4**: Phases 6-7 (commands + webhook)
5. **Week 5**: Phases 8-10 (reports, gateway facade, tests, deploy)
