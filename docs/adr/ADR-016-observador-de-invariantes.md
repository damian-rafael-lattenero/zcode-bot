# ADR-016 — Auditor: observador de invariantes entre componentes

**Estado:** aceptado (2026-10-15, F30, commit posterior a b74aac0)
**Disparo:** incidente 18:10-18:40 del 15/10 — 4 `/compact` y 2 turnos
fallaron en bucle con el mismo error y nadie lo correlacionó; Damian lo
detectó a mano, hora y media después, con un `/nuclear` de por medio.

## Contexto

El bot tiene auto-chequeos **intra**-componente: `orphans_check`
(turnos huérfanos), `stuck-watch` (sesión trabada), `outbox` (envíos
caídos), `housekeeping` (purge), `/nuclear` (reset con veredicto). Lo
que no tiene es nadie mirando las **fronteras**: estados que divergen
entre componentes, fallos repetidos que forman patrón, procesos sin
dueño. Cada incidente grande del 15/10 fue una impureza de frontera:

| Incidente | Impureza | Quién la vio |
|---|---|---|
| 15:47 429+deadlock | ráfaga de fallos de envío correlacionada | nadie (Damian) |
| 15:40 pop ajeno | registry ≠ procesos vivos | nadie (diagnóstico manual) |
| 18:10 timeouts | mismo fallo ×6 en 30 min | nadie (Damian al 4º) |
| 18:11 nuclear | marker de stop huérfano | nadie (rebote a las 18:13) |
| 18:10 zombie | Popen sin wait | nadie (deuda declarada) |

## Decisión

1. **Registro compartido de fallos** (`audit.py` → `state/bot-auditoria.json`,
   flock, cap 300): una línea por fallo `(ts, sid, tipo, detalle)` con
   `registrar()` que **jamás lanza** — vive en caminos de error y no
   puede empeorarlos. Deps mínimas (config + state, sin core) porque
   `utils/http.py` lo importa y http lo importa todos: un ciclo acá
   sería sistémico (gateado por test).
2. **Auditor en el supervisor del poller** (v1): corre en ambos drivers
   (`AsyncPoller` y `supervisor_loop`) como dep inyectada, con throttle
   propio (`auditor_cada_s`, 0 = apagado). Cada invariante es
   best-effort: una rota, las otras corren; ninguna lanza al supervisor.
3. **Filosofía**: SANA lo idempotente (consumir marker huérfano);
   RECOMIENDA lo dependiente de estado (sesión enferma → rotar o
   /task; fantasma → /stop go; cola → /qgo). Jamás ejecuta acciones
   destructivas por su cuenta.
4. **Anti-spam**: alertas con clave (`fallos|<sid>|<tipo>`,
   `fantasma|<pids>`, `cola|<sid>`) y cooldown por clave
   (`auditor_alerta_cooldown_min`, default 60) — la marca vive en el
   propio registro (tipo=alerta), no en memoria: sobrevive reloads.
5. **Zombie reap**: `_SPAWNED` + `_reapar_hijos()` en `_orphans_check`
   (`Popen.poll`, waitpid WNOHANG) — el poller reclama a sus hijos
   muertos cada ciclo.

## Consecuencias

- Detección de patrones en el **segundo** intento (vs. el cuarto de
  Damian a mano), y visibilidad de divergencias que ningún log
  individual muestra.
- El registro es nuevo estado compartido: cap 300 y entradas
  minusculas — el archivo no crece sin límite.
- Los knobs (`auditor_cada_s` / `auditor_fallos_min` /
  `auditor_alerta_cooldown_min`) son config en caliente como todo el
  bot; apagarlo es `auditor_cada_s: 0`.
- **Limitación honesta de v1**: vive en el poller — un poller
  congelado lo congela. Mitigado por el flock-deadline de e4d72fd (el
  poller ya no puede congelarse para siempre). Si volviera a pasar, la
  v2 es el mismo auditor como servicio launchd aparte (patrón
  watch-app), inmune al poller: +1 servicio, mismo módulo.

## Lo que NO es

- No es un LLM/agente inteligente: es un chequeo de invariantes
  determinista sobre archivos.
- No reemplaza orphans_check/stuck-watch: los complementa (ellos ven
  adentro de cada componente; el auditor, las costuras).
- No mide contratos del protocolo (duraciones, tamaños de respuesta):
  eso lo cubren los timeouts adaptativos de F29 y sus pwlogs.
