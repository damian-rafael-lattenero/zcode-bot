# ADR-003: Dos escritores (app ↔ bot) sobre las mismas sesiones

**Estado:** aceptada (2026-10-12)

## Decisión
Task ABIERTA en la app → su runtime es dueño del estado en memoria hasta
reabrirla; task CERRADA → el estado persistido en disco manda (el bot lo
lee/escribe headless). El bot NUNCA asume que la app verá sus cambios en
vivo, y avisa al usuario cuando corresponde (ej: "reabrí la task").

## Justificación
Compact desde el bot sobre una task abierta en la app: la app lo ABSORBE
(turn posterior re-persiste su copia en memoria) — incidente del 11/10.
La app no destruye el linaje compactado en disco (verificado), pero su
ventana sigue con el estado anterior hasta reabrir.

## Consecuencias
- Regla de usuario: compactar desde donde la task está abierta.
- El bot reporta números autoritativos del part `compaction`
  (preCompactTokenCount / truePostCompactTokenCount).
