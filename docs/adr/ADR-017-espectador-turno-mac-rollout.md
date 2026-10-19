# ADR-017 — Espectador: el turno de la Mac streamea por el rollout, no por el bus

**Estado**: aceptado (2026-10-19). Pedido de Damian: ver desde el celu lo
que hace un turno corrido en la app de la Mac (como en los turnos del
celu: tools, diffs, CoT). F32 cubre arranque y fin; F33 cubre el medio.

## Contexto — el bus está cerrado (spike 19/10)

La vía «natural» era suscribirse al stream de la sesión desde un
cliente espectador. Verificado EN VIVO contra un turno de app en vuelo:

1. `session/subscribe` (deliveryKind desktop-continuous) sobre la sesión
   de la app → **`"Session is not active"` (error -32004)**.
2. `session/events` desde un cliente fresco → 0 eventos, con el turno
   produciendo pasos en ese momento.
3. El código ya lo sabía: live_view.py (rama «sin deltas»): «su
   streaming interno no cruza al bot» (observación de F7).

El bus de eventos es del cliente que MANEJA el turno. Un espectador no
puede colgarse.

## Decisión

**Tail incremental del rollout** `~/.zcode/cli/rollout/model-io-<sid>.jsonl`
— el mismo archivo que F31 lee para /ctx. Verificado que registra TODO
turno (app o headless) con granularidad por request completado:
`response.toolCalls` (nombre + input completo → comando/diff),
`response.text`, `finishReason`, `startedAt/completedAt`, `turnId`,
`querySource`. Es puro archivo: el espectador no crea runtime ni cliente
de protocolo — más liviano que un worker (un proceso python leyendo).

- **Lectura**: offset propio; línea final sin `\n` no se consume (puede
  estar a mitad de escritura); registro > 8 MB se salta (el offset
  avanza igual); jamás se lee el archivo entero.
- **Espera reactiva**: `fswatch.wait_change` sobre el rollout + el
  marker del turno (kqueue, cero busy-wait — regla de la casa).
- **Correlación F20**: el primer registro con `startedAt ≥ t0` fija EL
  turnId; otros turnIds y `querySource != main_turn` (compact) no
  existen. Evita entregar turnos cruzados en sesiones largas.
- **Fin**: marker consumido (on_stop lo quita → F32 entrega la respuesta
  final) o cap duro 35 min. El espectador NUNCA manda el texto final
  (`finishReason == "tool-calls"` únicamente) — la no-duplicación es por
  construcción, no por coordinación de carreras.
- **Gates**: `mac_espectador` (master, default on), `tools_stream` +
  `tools_caja_filtro` (cajas), F15 (sesión con worker no spawnea: el
  worker ya streamea lo suyo), `_task_current_ok` (solo la task activa).

## Consecuencias

- Granularidad por PASO (el registro se escribe al completar el request),
  no por delta: no hay «tipeo en vivo» como en los turnos headless.
- **CoT no disponible para turnos de la app**: el runtime no persiste los
  thinking blocks — ni en el bus (cerrado) ni en el rollout (el request
  del registro siguiente viene sin ellos: el runtime los tira del
  historial para este provider). Si algún día aparecen, `_procesa` es el
  lugar.
- Costo: un proceso python durante los turnos de la Mac (lectura de
  archivo + sends). Sin/runtime, sin memoria del protocolo.
- Alternativa no adoptada: el remote control móvil OFICIAL de ZCode
  (versión nueva: login por cuenta + device management) cubre «mirar
  desde el celu» por la app de Z.ai. Se mantiene Telegram: sin cuenta
  de terceros, historial unificado, y todo el control-plane del bot.
