# ADR-013 — Live action: herramientas en vivo por polling incremental

Fecha: 2026-10-15 · Estado: aceptado · Supersede: nada (extiende F22bis)

## Contexto

El bot ya mostraba el CoT en vivo (deltas de `session/events`) y una caja
por tool COMPLETADA (F22bis, poll de `session/messages` cada 6 s). Damian
pidió ver "todo lo mismo que hago acá, pero en el celu": las tool calls,
los comandos de terminal y los diffs, **mientras pasan** — un live action
de la conversación.

Dos problemas de fondo:

1. **Latencia**: una tool aparecía recién completada (+6 s de poll).
2. **El poll de eventos no era incremental**: el server reenvía el replay
   COMPLETO en cada llamada (el schema rechaza `afterEventId` — ZodError
   verificado). Un turno real sobre una sesión grande midió `evs=815
   frame=24MB rss=3160MB cad=5s`: a 1 MB de tripear la guarda anti-OOM
   que apaga el CoT en vivo.

## Decisión

**Consumir el ciclo de vida de las tools del stream de eventos, con
polling incremental por `afterSeq`.** La evidencia es un spike contra el
app-server real (`scripts/spike_f7a.py`):

- El stream (`session/events`, envoltorio `model.streaming`/`tool.updated`)
  trae: `tool_input_start{toolCallId, toolName}` → `tool_input_delta{delta}`
  (el input JSON **tipeándose**) → `tool_call{input YA parseado}` →
  `tool.updated.started` → `tool.updated.result{success, content}`.
- `afterSeq` SÍ está en el schema y filtra `seq > afterSeq` (verificado:
  `afterSeq=32 → 0 eventos`). `seq` es un contador denso por sesión.
- `session/subscribe` devuelve `eventSeq` — cursor inicial que salta el
  replay de arranque.
- `session/messages` expone `part.callID`, la misma clave que
  `toolCallId`: emparejamiento exacto stream ↔ historial.

### Alternativas descartadas

- **Capturar pushes `session/event` en ProtoClient** (plan B del spike):
  el poll incremental da la misma información con latencia ≤2 s sin
  tocar el lector de pushes (que descarta params a propósito: los
  `session/update` del resume traen MBs). Reevaluar si la latencia
  molesta.
- **Seguir con el replay completo + dedupe**: es el statu quo que infló
  la RSS a 3.1 GB. Las guardas anti-OOM quedan como red de seguridad,
  no como modo normal.

## Implementación (F7)

- `LiveTurnView._poll_events`: request con `afterSeq=last_seq` (cursor =
  máximo `seq` visto; inicial: `eventSeq` del subscribe que pasa
  `prompt_worker`); dispatch de los kinds de tool a `_tool_evento`.
- Caja por tool: nace `🔵` al `tool_call` (input parseado → `_tool_caja`,
  comando completo/diff real), se **edita** a `✅`/`❌` con
  `tool.updated.result{content}`. La tool corriente se ve en la caja viva
  (tipeándose / `🔵 corriendo`).
- Fallback: si el stream no cerró una caja (events_off a mitad de tool),
  el poll de mensajes la cierra con la salida del historial; las tools
  con caja viva no se duplican (`boxed`, por `callID`). Con `events_off`
  el comportamiento es el F22bis de siempre.
- Config: `tools_stream` apaga todo el live de tools; `segundo_plano`
  (F15) sigue silenciando cajas.

## Consecuencias

- Frames de KBs en vez de MBs; el RSS del worker deja de inflarse con el
  historial de la sesión; las guardas 25 MB/1200 eventos/2 MB siguen
  como red ante replays inesperados.
- Un turno tool-heavy manda ~1 caja por tool (~1 msg/s con pace) — Damian
  eligió "todas las tools" a sabiendas; un filtro por tool es trivial si
  el volumen molesta.
- Declarado follow-up (fuera de alcance): `turn.completed`/`turn.failed`
  y `permission.requested` por eventos — reemplazarían el escaneo de
  logs JSONL de `_turn_end_poll`. **Cerrado en F9** (ADR-015): el fin
  del turno va por el stream con correlación por `inputId`; los
  permisos ya eran push (`interaction/requestPermission`).
