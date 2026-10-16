# ADR-009 — Suite reactiva: tests asyncio sin sleeps, producción con esperas reactivas y TTL

**Estado**: aceptado (2026-10-15)
**Contexto**: la suite coordinaba hilos con `sleep(0.3)`/`sleep(3.5)` y el
código de producción tenía esperas imperativas (polling con sueño fijo en
`ProtoClient.request`, carrera de armado en `fswatch`, `while True` con
`sleep` en el reader y en `_wait_cli_event`). Los tests eran lentos y
flaky: el sleep tenía que ser "suficientemente largo" para el hardware del
día. Damian fijó la doctrina: *todo reactivo, nada de sleeps, las esperas
con TTL — "como una mónada: todo el flujo del test compone"*.

## Decisión

**Tests**: toda la suite es `unittest.IsolatedAsyncioTestCase` (stdlib
3.9) con el kit `tests/asynkit.py` — una primitiva por responsabilidad:

- `call(fn, ...)` — bloqueante del SUT → `asyncio.to_thread`; se espera
  con `await`.
- `wait_until(pred, timeout, msg)` — rendezvous por condición: gira con
  `sleep(0)` (yield al loop, NO una espera temporizada). Determinismo:
  el predicado corre primero y CUALQUIER orden productor/consumidor
  converge sin coordinación temporal; vencido el TTL → AssertionError.
- `Sig` — puente hilo→loop (`call_soon_threadsafe`).
- `Chan` — canal acotado productor→consumidor (`asyncio.Queue` con
  `join()`/`task_done()` para pools); los tests escalan linealmente
  agregando corrutinas al `gather` (N productores × M resolvedores,
  test_concurrency), sin tocar el kit.

**Producción** (sigue threads + kqueue + archivos por ADR-008; lo que
cambió es CÓMO se espera):

1. `ProtoClient.request` espera sobre `threading.Condition` (despierta
   por `notify_all` en `_dispatch`; muerte del server → flag `dead` +
   notify). El reader es un generador puro (`_frames`) sobre
   `select.select` con slice de 0.25 s — el data-flow es declarativo.
2. `fswatch` cierra la carrera de armado: snapshot baseline ANTES de
   armar el kqueue + escaneo de seguridad en cada slice vacío. Y la
   primitiva canónica de espera con predicado es
   `fswatch.wait_for(pred, paths, timeout)`: chequea el predicado
   primero y bloquea en el kernel hasta evento o TTL.
3. `_wait_cli_event` (gateway) usa `fswatch.wait_for`; `cmd_register`
   usa long-poll como espera reactiva.

**Sleeps residuales LEGÍTIMOS** (no son busy-waiting; cada uno es una
política, no una coordinación):

| Categoría | Ejemplos |
|---|---|
| Retry con backoff | send 0/1/3 s, 5 s ante 429 |
| Pacing anti-flood Telegram | ~1 msg/s CoT (1.2 s), ediciones ≥3 s |
| Settle-time post-evento | nuclear `sleep(2)` pre-kickstart (`ctx_probe` 1.5 s, retirado en F31) |
| TTL con espera inyectable | `_nuc_wait` (camino del nuclear; la versión 100% reactiva sería kqueue `EVFILT_PROC` — riesgo/beneficio desfavorable en el camino de emergencia) |
| Cadencia de monitores REPORTING | `cmd_stuck_watch`, `cmd_watch_app` |
| Fallback sin kqueue | `fswatch._wait_poll` |

## Consecuencias

- Suite determinista: cero flakiness de coordinación; los tiempos solo
  aparecen como aserciones de cota superior (<1.5 s, <2 s).
- ACID de los tests: Atomic (fixture fresh por test, `support.fresh`),
  Consistent (sin wall-clock en aserciones), Isolated (loop nuevo por
  test + tempdirs + rebind de QUESTIONS/WORKERS en support), Durable
  (asserts sobre archivos committeados).
- Regla anti-regresión: un `time.sleep` nuevo en tests/ es bug; en src/
  debe caer en una de las categorías de arriba (o en una nueva con ADR).
- Pendiente documentado: `EVFILT_PROC` para `_nuc_wait` (se re-evalúa
  si el nuclear vuelve a necesitar precisión sub-segundo).
