# ADR-001: Un solo lector de getUpdates por bot

**Estado:** aceptada (2026-10-12) · **Contexto:** bot @glm_lattenero_2_bot

## Decisión
El poller (LaunchAgent com.zcode.telegram-cmds) es el ÚNICO consumidor de
getUpdates del 2_bot. Los procesos hook que esperan respuestas (permisos 🔐,
preguntas ❓) coordinan mediante el flag `state/tg-busy` (mtime): mientras
está fresco, el poller no consulta getUpdates.

## Justificación
- Oficial: getUpdates "will not work if an outgoing webhook is set up" y dos
  consumidores simultáneos chocan (409 Conflict) — validado empíricamente al
  inicio del proyecto contra el bot integrado. Ver docs/REFERENCES.md #2.
- El offset es compartido (state/tg-offset): dos lectores se roban updates.

## Consecuencias
- La ventana del flag debe cubrir TODO el wait: deriva de wait_timeout_s+60
  (fix D2; antes 120 fijo con waits de 240 → carrera).
- El bot integrado (@glm_lattenero_bot) usa su propio token: nunca cruzar.
