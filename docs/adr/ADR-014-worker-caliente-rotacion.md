# ADR-014 — Respuesta instantánea: worker caliente + rotación de sesión

Fecha: 15/10/2026 · Estado: implementado (F8)

## Contexto

El ciclo del bot era **un proceso por turno**: cada mensaje spawneaba un
worker que arrancaba un runtime app-server y pagaba `session/resume`. En
una sesión grande ese resume devuelve el transcripto COMPLETO y no se usa
para nada (`_proto_ready` solo chequea el error): **27.5MB / 3737 mensajes
≈ 36.7s medidos**. Los skips de F7++ dejaron el arranque en un resume
(~35s) — que crece para siempre con la sesión.

Candidato obvio descartado con evidencia: **`compact` NO achica el
transcripto** — el 15/10 hubo 4 `compact.completed` sobre la sesión F4+F7
(10:34, 12:22, 14:39, 16:35 manual) y el resume siguió en 27.5MB: compact
achica el CONTEXTO del modelo, no el historial ni la respuesta de resume.

Spike (`/tmp/spike-ws`): sin resume previo, `send`/`setModel`/
`session/messages` fallan con `-32004 "Session is not active"` — el
resume es el loader, obligatorio por conexión. No hay forma de saltearlo
en un proceso nuevo.

Web (pedido de Damian: "buscaste en la web?"): el protocolo es el mismo
diseño que el Agent SDK de Claude Code. Doc oficial de sesiones: no
existe resume liviano ni trimming ni `contentProfile` para resume;
`forkSession` COPIA el historial. El patrón recomendado para resumes
pesados es arquitectural: **sesiones cortas + memoria sembrada** y
mantener el estado vivo (equivalente a `previous_response_id` de la
Responses API de OpenAI: mandás solo el mensaje nuevo). Anthropic
publicó un postmortem (abr-2026) admitiendo este mismo problema (recorte
de thinking viejo en sesiones idle para bajar latencia de arranque).

## Decisión (elegido por Damian: A + B)

1. **Worker caliente** — el runtime sobrevive al turno: `_correr_turno`
   (un turno sobre sesión activa, fría o caliente) + `_warm_wait`
   (espera del próximo prompt de ESA sesión: marker de /stop, cola,
   heartbeat al registry, timeout `warm_keepalive_s` default 600, `0` =
   comportamiento clásico). El pickup del ítem encolado corre sobre la
   MISMA conexión: cero resume. La carrera de dispatch ya la cubre el
   registry (el mensaje llega → spawn del dup → ve entrada viva → encola
   → pickup ≤1s). `/stop` frena en <1s por marker (dentro de la gracia
   de 4s: sin kill); `/stop go` sigue disponible. El índice `waiting` en
   la entrada distingue el worker esperando.
2. **Rotación automática** — con la cola vacía (jamás bloquea un mensaje)
   y el transcripto (`cli/rollout/model-io-<sid>.jsonl`) sobre
   `rotar_umbral_mb` (default 8, `0` = off): compact → turno de resumen
   en la sesión compactada → `session/create` en runtime NUEVO → send
   semilla con el resumen → swap de `bot-active` + rekey de registry y
   cola + `_restore_model` de la vieja. La vieja queda archivada
   (accesible por `/task`); el arranque en frío vuelve a ~5s. Cualquier
   fallo → aviso y continuidad en la sesión vieja.

## Consecuencias

- El picker de modelo de la app puede mostrar zai-bot mientras el worker
  está caliente (ventana ≤ keepalive; hoy ya pasa durante turnos largos).
- `/retry` durante el warm avisa "ya hay un turno corriendo" aunque solo
  esté esperando — wart aceptado (documentado).
- El warm default (600s) viene de `config.DEFAULTS`; los tests lo
  pisan a 0 en `support.load_module` (un solo lugar).
- Los turnos del warm reusan `act` del ítem encolado (modelo/modo
  frescos por si `/task` cambió algo mientras esperaba).

## Fuera de scope

- Ruta express sin agente (respuesta directa por API para chat simple).
- Daemon multi-sesión centralizado (el warm por sesión logra lo mismo
  sin romper el aislamiento F16).
