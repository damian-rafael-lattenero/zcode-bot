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
`/stop` frena cooperativo y **escala solo**: si el runtime no muere en 4 s,
kill -9 al grupo (worker + app-server); `/stop go` lo mata al instante.

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

Canillas: `cot_stream` / `tools_stream` (default ON), `live_progress`,
`tools_caja_filtro` (`accion` | `todas` | `minimo`).
Infra: poll de eventos **incremental** (`afterSeq`, cursor inicial del
`eventSeq` del subscribe — el replay completo infló un turno real a 3.1GB
RSS); guardas anti-OOM como red de seguridad; emparejamiento
stream↔historial por `callID`/`toolCallId`. Detalles: ADR-013.
El diff de cada Edit trae diffstat arriba: ● 🟢 +agregadas  🔴 −borradas
(pedido de Damian — el total de un vistazo antes del detalle línea a
línea; MultiEdit lo repite por cada edit).

Arranque rápido: el placeholder «arrancando…» sale en ~2s (antes de tocar
el runtime) y los sets no-op se saltan (`setModel`/`setMode` devuelven el
historial COMPLETO por request — 26MB ≈ 30s en una sesión grande; el
modelo se valida contra el sqlite, el modo contra `mode_set`). Una sesión
muy grande igual paga el `resume`: la **rotación** (abajo) la evita.

## Respuesta instantánea (F8 — worker caliente + rotación)

El patrón de los productos que contestan al toque: **el estado queda
vivo** y **las sesiones son cortas** (ADR-014, con las fuentes).

- **Worker caliente**: tras entregar la respuesta, el runtime NO muere —
  queda esperando el próximo mensaje de esa sesión hasta
  `warm_keepalive_s` (default 600, `0` = un proceso por turno, como
  siempre). Mensajes seguidos arrancan **al instante**: cero resume, cero
  «arrancando». El heartbeat al registry renueva la entrada (waiting=True)
  y `/stop` lo frena en <1s sin necesidad del kill. Mientras espera, el
  picker de modelo de la app muestra la sesión con zai-bot (ventana
  acotada al keepalive — el mismo estado de hoy durante un turno largo).
- **Rotación automática**: si el transcripto de la sesión
  (`cli/rollout/model-io-<sid>.jsonl`) supera `rotar_umbral_mb`
  (default 8, `0` = off) al quedar caliente con la cola vacía, corre la
  danza: `compact` → turno de resumen → **sesión nueva sembrada** con ese
  resumen → swap de la sesión activa (rekey de registry y cola; la vieja
  queda archivada, accesible por `/task`). El compact NO achica el
  transcripto (medido: 4 compacts y el resume siguió en 27.5MB) — la
  rotación sí: el arranque en frío vuelve a ~5s para siempre. Cualquier
  paso que falle → aviso y continuidad en la sesión vieja (jamás se
  pierde el hilo). Los transcriptos de sesiones ya no referenciadas se
  purgan a los `rollout_retain_dias` (default 7, `0` = off; F9 — la
  housekeeping: cada rotación dejaba 44MB atrás).

## Fin del turno por eventos (F9 — ADR-015)

`_turn_end_poll` ya no escanea los logs JSONL del CLI como camino
principal: el fin llega por el **stream** `session/events` (poll
incremental `afterSeq`, KBs). El stream no trae `turnId` — la
correlación F20 se rehace por `inputId` (spike `scripts/spike_f9_turnfin.py`;
gate de decisión). El subscribe se hace ANTES del `send`, siempre, así
el cursor garantiza que los eventos del turno entren. Cualquier falla
del stream (conn muerto, frame >25MB) degrada 1:1 al escaneo de logs,
que queda como red (y sigue siendo el único camino del supervisor de
huérfanos). Los permisos ya eran push
(`interaction/requestPermission`). Cierre del follow-up de ADR-013;
`commands/dispatch.py` dividido en `handlers_tareas` / `handlers_info` /
`handlers_sistema` (deuda ADR-012) con `support.parchea` + gate
anti-rebinds-planos (`test_higiene`).

## Auditor de invariantes (F30 — ADR-016)

El observador de fronteras que faltaba: orphans_check, stuck-watch y
outbox son auto-chequeos **intra**-componente; el auditor mira las
costuras entre componentes como caja negra, por archivos, sin locks ni
camino caliente. Corre en el supervisor del poller con throttle propio
(`auditor_cada_s`, default 30; **0 = apagado**). Cuatro invariantes,
todas nacidas de incidentes reales:

1. **Correlación de fallos** — `≥ auditor_fallos_min` (default 2)
   fallos del mismo tipo+sesión en 10 min → «sesión enferma, /task a
   otra o esperar rotación». Los componentes registran sus fallos en
   `state/bot-auditoria.json` (`audit.registrar`: proto, send,
   turn-failed, crash, runtime, envio, handler); el bucket global
   cubre ráfagas de envío (el 429/deadlock del 15/10 15:47 habría
   alertado solo). Con esto, el incidente 18:10 se detecta al segundo
   intento, no al cuarto.
2. **Markers de /stop huérfanos** — sin worker ni turno que los deba
   (y con >60s de edad, anti-carrera con spawn): se consumen en
   silencio. El nuclear SIGTERM crea el marker en el handler y el
   SIGKILL posterior impide consumirlo → frenaba el próximo turno de
   rebote (18:13, worker 13066).
3. **Workers fantasma** — run-turn vivo sin entrada en registry →
   alerta con /stop go (registry vacío = /stop, /status y stuck-watch
   ciegos, incidente 15:40). Recomendación, no kill.
4. **Cola dormida** — items encolados >10 min sin worker y sin corrida
   en vuelo → «/qgo o /task». El F17 normal (Mac ocupada) NO alerta;
   los items legacy sin `ts` tampoco.

Anti-spam: una alerta por clave cada `auditor_alerta_cooldown_min`
(default 60). Filosofía: **sana lo idempotente, recomienda lo
dependiente de estado** — jamás ejecuta acciones destructivas. Bonus:
el poller ahora reapa sus Popens hijos cada ciclo (fin de los zombies
`<defunct>`, deuda del incidente 18:10).

## /ctx honesto (F31)

El e2e del 15/10 dejó un falso negativo: `/compact` funcionó (el
runtime logueó `compact.completed`, 185k → 23k tokens en el siguiente
request) pero `/ctx` siguió mostrando 185k. Dos causas, las dos
reparadas: (1) el probe `ctx_probe.py` hacía `session/resume` con
timeout fijo de 20 s (la misma clase de bug de F29) y además el runtime
**no persiste** la proyección — `contextUsed` era 0 siempre, con lo que
/ctx nunca mostró un número medido y pagaba un resume de 35 s / 3 GB
por consulta; (2) `_contexto_ktokens` leía 64 KB de cola, pero cada
registro del model-io jsonl pesa lo que el contexto (200 KB+), así que
el parse fallaba justo en las sesiones grandes y `/compact` caía al
timeout base. Ahora `core.ultimo_uso(sid)` lee el rollout con ventana
creciente y devuelve `(ktokens, epoch, fuente)`; `/ctx` muestra ese
número en 0,05 s y, si el último `compact.completed` es más nuevo que
el último request, avisa «🧹 compactada HH:MM — el tamaño real se ve en
el próximo turno (antes N)» en vez de repetir el número viejo. El probe
queda retirado.

## El turno de la Mac llega completo (F32)

Pedido de Damian: los avisos de los turnos corridos desde la app de la
Mac eran vistas previas (prompt 200 chars, respuesta 500). Ahora los
hooks mandan el **texto completo** troceado al límite de Telegram
(trozos de 3800, hasta 6 partes; si excede, la última marca el recorte —
el íntegro queda en la sesión, `/cot`). La cabecera trae lo pertinente:
nombre de la task (🧵, del índice de tasks; fallback al workspace),
pasos (🧰 `toolCallCount`), duración (⏱, mtime del marker) y workspace
(📁). Una sola notificación: primera parte con sonido, colas mudas.
Gates intactos: `notify_prompt`/`notify_stop`, F15 (sesión con worker
del bot no duplica) y `_task_current_ok`. +6 tests.

## El turno de la Mac en vivo: espectador (F33 — ADR-017)

F32 manda arranque y fin de los turnos corridos en la app de la Mac;
F33 agrega **lo del medio**: un proceso desprendido (el «espectador»,
spawneado por `on_user_prompt` junto al stuck-watch) hace tail
incremental del rollout `model-io-<sid>.jsonl` y manda a Telegram, con
el formato de siempre, cada paso del modelo: caja de tool call (comando
completo / diff real), narración intermedia, rótulo inicial
«🖥️ turno de la Mac en vivo». La respuesta final NO la manda el
espectador: la entrega el Stop hook de F32 (el espectador se apaga
cuando el marker se consume — cero duplicación por construcción).

Por qué no el stream de eventos: es del cliente que maneja el turno —
`session/subscribe` sobre una sesión de la app responde «Session is not
active» (spike 19/10). El rollout es el mismo archivo que F31 lee para
/ctx: registro por request completado, sin runtime ni protocolo.
Límite honesto: granularidad por paso (no tipeo en vivo) y **sin CoT** —
el runtime no persiste los thinking blocks ni en el bus ni en el
rollout. Knob `mac_espectador` apaga todo; `tools_stream`/
`tools_caja_filtro`/gate F15 siguen mandando. +17 tests.

## Bitácora de un e2e (cómo se ve un turno desde el celu)

Turno real del 15/10/2026 (commit `1aa9620`, 342 tests, worker caliente
activo) — lo que vio Damian en el chat, paso a paso:

1. **Mensaje del celu**: «que te haga una pregunta, trabajes según la
   respuesta, ejecutes un comando y modifiques el readme».
2. **Caja instantánea**: 🧠 «testing» arrancando… → abriendo sesión →
   sesión lista — enviando… (progreso real, no una estatua).
3. **CoT en vivo**: mensajes «CoT N» scrolleables con el razonamiento
   completo, intercalados con la caja viva de la tool corriente.
4. **❓ pregunta interactiva**: el agente preguntó qué agregar al README
   con 3 opciones — Damian respondió tocando la opción en el celu
   (`AskUserQuestion` → ❓ headless → respuesta vuelve al runtime).
5. **Tools visibles**: caja 🔵 por cada tool que **hace** algo (preset
   `accion`) con comando tipeándose en vivo, que se edita a ✅ con la
   salida; Read/Grep solo en el índice de pasos (bookkeeping no ensucia).
6. **Diff a lo git diff**: la edición del README llegó como caja con
   diffstat `● 🟢 +N 🔴 −N` arriba y las líneas 🟢/🔴 debajo, una por
   línea.
7. **Entrega**: ✅ «testing» · respuesta: + el resumen final del agente.

En ese turno el worker quedó **caliente** (F8): el mensaje siguiente de
Damian no mostró «arrancando» — arrancó al instante sobre el mismo
runtime.

## Knobs: qué se puede tunear (F34, ADR-018)

Todo knob vive SOLO en `DEFAULTS` (config.py) y se cambia sin deploy
editando `~/.zcode/telegram-remote.json`. Los valores compartidos entre
módulos viven en `limites.py`; los de dominio, en su módulo. El gate
(`scripts/check_no_duplicates.py`) enforcea: un símbolo un cuerpo,
`config.X` existente, y ningún literal semántico sin nombre fuera de
allowlist.

| Knob | Default | Qué hace |
|---|---|---|
| `wait_timeout_s` | 240 | espera de respuestas del chat (permisos, /deploy) |
| `question_wait_timeout_s` | 60 | espera de AskUserQuestion en modo phone |
| `question_mode` | mac | pregunta: `mac` avisa y responde en la Mac; `phone` espera al celu |
| `stuck_after_min` | 20 | minutos de turno trabado antes del aviso |
| `loud_all` | True | notificaciones con sonido salvo loud=False explícito |
| `suppress_error_patterns` | 5 regex | fallos que no notifican (ruido conocido) |
| `notify_prompt` / `notify_stop` / `notify_failures` / `notify_session` | True | avisos de hooks de la Mac (F32/F15) |
| `only_active_task` | True | solo la task activa empuja al chat (F15) |
| `warm_keepalive_s` | 600 | minutos de worker caliente (0 = proceso por turno) |
| `rotar_umbral_mb` | 8 | MB del rollout que disparan rotación de sesión |
| `auditor_cada_s` / `auditor_fallos_min` / `auditor_alerta_cooldown_min` | 30 / 2 / 60 | cadencia y sensibilidad del auditor (F30; 0 apaga) |
| `rollout_retain_dias` | 7 | días de retención de rollouts viejos |
| `mac_espectador` | True | streaming en vivo de los turnos de la Mac (F33) |
| `mac_cot_botones` | True | el CoT de los turnos de la Mac EN VIVO: caja «pensando…» cuyo teclado crece un botón por bloque; al cerrar, texto final (F40/F41) |
| `cot_stream` / `tools_stream` / `tools_caja_filtro` / `live_progress` | True / True / accion / True | qué streamea un turno del celu (F7) |
| `cot_botones` | True | el CoT de los turnos del CELU como botones que crecen por fase de pensado (reemplaza el texto de cot_stream — F42) |
| `trace_documents` | False | /cot guarda el documento del turno |
| `headless_guard` | True | guardrail anti-suicidio en prompts headless |
| `transport` | polling | transporte de updates (webhook requiere ADR-011) |
| `webhook_url` / `webhook_secret` | "" / "" | los escribe /setwebhook |

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
