# ADR-008 — Optimización de memoria y velocidad: patrones aplicados y descartados

**Estado**: aceptado (2026-10-13)
**Contexto**: tras el incidente OOM real (poller a 2.9GB → macOS SIGKILL →
turno perdido), Damian pidió auditar el código contra los patrones de
optimización espacial/velocidad conocidos y decidir cuáles encajan.
Principio rector del proyecto: patrón con nombre + fuente, nada inventado.

## Aplicados (patrón → dónde → qué protege)

1. **Circuit Breaker** (learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker)
   → `EVENTS_TRIP` en `_live_view`: si el replay de `session/events` supera
   1200 eventos, el stream en vivo se APAGA para el resto del turno y la
   caja muestra estado honesto (degradación, no muerte). Previene la
   repetición de una operación cara que ya demostró ser letal.
2. **Bulkhead / aislamiento por pool**
   (learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead)
   → `ctx_probe.py` como subproceso desechable: un resume de sesión
   gigante infla ESE proceso, nunca el poller. Mismo espíritu: los workers
   viven en threads separados del loop de comandos.
3. **Liberación del prefijo consumido** (semántica de offset de consumidor
   — Kafka docs: el consumidor avanza su offset y el log retiene lo ya
   consumido) → `cot_flush`: `cot_full = cot_full[nf:]` tras mandar cada
   caja. Lo ya entregado al chat NO se retiene en memoria.
4. **Sliding window / buffers acotados** → `evs[-400:]` (ventana de
   observación), colas de 3200 chars en la caja viva, `tail_bytes=512KB`
   en `_turn_end_poll` (leer solo la cola del archivo, no slurp).
5. **Dedupe cache + skip de payloads grandes** → `vistos` por eventId +
   descarte de eventos >30KB (snapshots): lo acumulable jamás crece con el
   tamaño del payload.
6. **Retry con backoff + rate limiting** (Telegram FAQ ~1 msg/s por chat;
   reintentos 0/1/3 en `send`, sleep 5s ante 429, pausa 1.2s entre cajas
   de CoT, ediciones ≥3s) → progresión lenta, nunca ráfaga.
7. **Tail-following con generadores** → `cli_logs.iter_events` (yield por
   evento, needle-filter antes de json.loads) y cola de 512KB del log.
8. **Pool de conexiones por unidad de trabajo** → UN `ProtoClient` por
   worker (no por request); cerrado en `finally` (RAII, ADR de proto.py).

## Auditados y DESCARTADOS (con razón)

- **`"".join(list)` en vez de `+=` para acumular deltas**: en CPython,
  `str += x` con la única referencia es amortizado O(1) (optimización
  documentada del intérprete — bugs.python.org/issue980695; PyPy no la
  tiene, pypy.org/posts/2023/01/string-concatenation-quadratic.html, pero
  corremos CPython 3.9 del sistema). Nuestros buffers (razon/texto/cot_full)
  son locales con refcount 1 → cambiar a join-suma complejidad sin ganancia.
- **Watermark O(1) en vez del set `vistos`**: los eventId no están
  garantizados monótonos (dedupe defensivo contra replay); el set es de
  strings chicas (MB a lo sumo en sesiones enormes) — no justifica el
  riesgo de perder deltas.
- **Caché de resultados de /ctx con TTL**: ya existe la alternativa mejor
  (probe por subprocess, bulkhead); cachear en el poller reintroduce el
  riesgo de retener payloads.
- **Compresión de estado en disco**: archivos de state son KBs — costo de
  complejidad > beneficio.
- **asyncio / threads de I/O dedicados**: stdlib-only + ciclo de 10s del
  poller; el I/O real (Telegram) ya es ~1/s. Reescribir para "velocidad"
  que no es cuello de botella.

## La causa raíz del OOM (lección para el futuro)

`session/events` devuelve el replay COMPLETO de la sesión (sin cursor,
Zod strict — re-probado). El costo no era retener (dedupe funcionaba): era
TRANSITORIO — json.loads del replay entero cada 2s; pymalloc crece arenas
y no las devuelve al SO → ru_maxrss sube y macOS mata. Por eso el fix
correcto es no pedirlo (circuit breaker), no "liberar mejor".

## Verificación

`ru_maxrss` se loguea en cada fin de turno (pwlog "fin: ... rss=%dMB");
el poller idle hoy: ~20MB. Regresión viva: si un turno de "testing" vuelve
a reportar >1GB, el trip no alcanzó → bajar EVENTS_TRIP.
