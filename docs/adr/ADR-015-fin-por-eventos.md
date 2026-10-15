# ADR-015 — Fin del turno por eventos (el stream reemplaza al escaneo de logs)

Fecha: 2026-10-15 · Estado: aceptado · Supersede: el follow-up declarado en
ADR-013 · Gate de decisión: `scripts/spike_f9_turnfin.py`

## Contexto

`_turn_end_poll` (el thread que espera el fin del turno del worker)
detectaba `turn.completed`/`turn.failed` escaneando los logs JSONL del CLI
(cola de 512KB, throttle 1s, despertares por fswatch). Funcionaba, pero era
un canal secundario: texto que el CLI escribe de más, que el worker parsea
de más, en cada vuelta del turno. ADR-013 lo dejó declarado como follow-up:
"por eventos — reemplazarían el escaneo de logs JSONL".

## Decisión

**El fin del turno se consume del stream `session/events`, con el escaneo
de logs como fallback permanente.** Evidencia del spike:

- El stream trae el ciclo de vida del TURNO con `type` `turn.started`
  (payload: `inputId`, `input`, `executionStartedAt` en ms) y
  `turn.completed`/`turn.failed` (payload: `inputId`, `resultType`,
  `response`, `usage`, `duration`).
- **El stream NO trae turnId** (la clave de correlación de los logs): la
  correlación F20 se rehace con `inputId`, compartido entre el
  `turn.started` y su fin. El primer `turn.started` posterior al
  `session/send` fija `mios`; solo el fin con ESE inputId vale (un
  `completed` ajeno sin `mios` propio no se acepta jamás con `t_send`).
- El buffer NO repite eventos viejos a un suscriptor nuevo: `subscribe`
  devuelve `eventSeq` (cursor) y `afterSeq` filtra `seq > cursor` — poll
  incremental en KBs, la misma disciplina anti-OOM de ADR-013.

## Implementación (F9)

- `live_view._fin_de_stream`: un poll del stream → `(fin, mios, last_seq)`;
  normaliza al shape de los logs (`{"event": ..., "error": {...}}`) para
  no tocar a ningún consumidor.
- `_turn_end_poll(conn=, start_seq=)`: con conexión+cursor va por stream;
  cualquier falla (request en error, conn muerto, frame >25MB = replay
  roto) degrada 1:1 al camino de logs, que sigue siendo el ÚNICO camino
  del supervisor de huérfanos (ve turnos de workers ya muertos, sin
  conexión) y del modo sin subscribe.
- `workers._correr_turno`: el `subscribe` se hace ANTES del `send`,
  SIEMPRE (antes era solo para el live y después del send) — el cursor
  garantiza que los eventos del turno entren en el stream. Los dos
  `_turn_end_poll` de la rotación (resumen y seed) van por el mismo camino.
- `permission.requested`: sin cambios y a propósito — ya era push
  (server→client `interaction/requestPermission` → callback
  `on_permission` del ProtoClient). En el stream no aparece como kind.
- `zcode_proto.py` intacto: el lector de pushes sigue descartando params a
  propósito (los `session/update` del resume traen MBs).

## Consecuencias

- El fin del turno deja de depender de que el CLI escriba su log (y del
  throttle del escaneo): llega por el mismo canal que el resto del turno.
- El fallback a logs queda como red, no como modo normal — si el stream
  falla NUNCA se pierde el fin: se degrada y sigue.
- Un `turn.completed` del stream trae `resultType` → un turno que termina
  "completed" con error se mapea a `turn.failed` honesto.
- Tests: `tests/test_f9_eventos.py` (correlación, peer ajeno, degradaciones).
