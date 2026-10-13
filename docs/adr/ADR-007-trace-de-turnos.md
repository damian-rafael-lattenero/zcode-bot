# ADR-007 — Trace de turnos: pasos + CoT completo vía documento de Telegram

**Estado**: aceptado (2026-10-12)
**Contexto**: Damian quiere leer el paso a paso COMPLETO de cada turno
(razonamiento de principio a fin, comandos, archivos) scrolleando en el
chat. La vista en vivo edita UN mensaje con la cola — lo anterior se
"pierde" visualmente — y el CoT de un turno no entra en mensajes de 4096
chars (un turno real: 115K chars de razonamiento, 49 tool calls).

## Hechos que lo hacen posible

- Cada paso del turno persiste como mensaje assistant con parts
  `step-start / reasoning / text / tool / step-finish` (verificado en vivo
  contra el historial de una sesión real). El CoT COMPLETO está en disco.
- `session/messages {afterMessageId}` entrega los pasos a medida que
  completan → los 🔧 pasos se pueden mostrar DURANTE el turno.
- Leer el historial SÍ cruza runtimes (frenar no cruza, ADR-002): /cot
  funciona también para tasks corridas en la app de la Mac.
- `sendDocument` (Bot API) acepta .txt vía multipart/form-data — el visor
  de Telegram lo scrollea entero; 4096 chars deja de ser límite.

## Decisión

1. **Durante el turno** (`_live_view` v2): mensaje «🔧 pasos» que crece
   (poll afterMessageId cada 6 s, últimos 12 visibles) + cola del
   razonamiento ampliada a 3200 chars. Cero mensajes extra por paso
   (rate limits: se edita, no se envía).
2. **Al terminar** (`_turn_trace`): documento .txt con PASOS numerados +
   RAZONAMIENTO COMPLETO + RESPUESTA, autoritativo desde el historial
   (buffers vivos solo como fallback). Persiste en
   `state/last-turn-trace.txt`. Config `trace_documents=false` apaga el
   documento (el archivo queda).
3. **`/cot [n|@sid]`** (alias /trace): trace on-demand de cualquier task
   (últimos ~150 mensajes) — también para las corridas en la app.

## Consecuencias

- El CoT completo queda disponible SIEMPRE (disco + documento), incluso
   si el streaming en vivo falló o la task corrió en la Mac.
- Alternativa descartada: chunk-flush de mensajes en vivo (el CoT en
  mensajes de 3900) — 115K chars = ~30 mensajes por turno: demasiado
  ruido y riesgo con los rate limits; el documento lo reemplaza.
- El trace del turno refleja lo persistido: en tasks abiertas en la app
  aplica la divergencia de dos escritores (ADR-003).
