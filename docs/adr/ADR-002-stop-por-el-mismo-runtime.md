# ADR-002: /stop frena por el MISMO runtime del worker

**Estado:** aceptada (2026-10-12)

## Decisión
`/stop` (solo corridas iniciadas desde el celular) envía `session/stop`
con el cliente ProtoClient DEL worker que corre el turno (registrado en
`WorkersRegistry`), nunca con un runtime nuevo.

## Justificación
Un runtime NO ve los turnos de otro (probado en vivo: sesión corriendo en la
app aparece "idle" para un runtime headless; 9 métodos de stop cruzado →
-32601). Un runtime nuevo para frenar no alcanza el turno y encima lo
diagnosticaba mal como "turno de la app". REFERENCES.md #12.

## Consecuencias
- Requiere ProtoClient thread-safe (requests concurrentes): wlock serializa
  id-alloc + escrituras a stdin; `Lock` NO es reentrante — nunca anidar
  (deadlock del 12/10, regresión testeada con servidor fake concurrente).
- Turnos de la app: fuera del alcance de /stop (se frenan con el botón Stop
  de la app; input sintético en macOS exige TCC, no hay vía sin permisos).
