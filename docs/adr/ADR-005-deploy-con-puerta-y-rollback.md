# ADR-005: Deploy con puerta reload-now, backup y rollback

**Estado:** aceptada (2026-10-12)

## Decisión
`deploy.sh`: tests → backup timestamped → copia → py_compile → ritual
(running_turns == 1 en gateway) → import check del paquete → puerta
`state/reload-now` → verificación de PID nuevo. Rollback: `--rollback`
restaura el último backup + kickstart.

## Justificación
- El poller NUNCA se reinicia con corridas del bot en vuelo (la puerta
  espera HEADLESS_WORKERS vacío): no se pierden entregas.
- Ritual nacido de incidentes reales (ediciones a medias que launchd
  ejecutaba → IndentationError en loop).

## Consecuencias
- Deploy seguro incluso con el bot en uso; el gate es autónomo.
- LaunchAgent y paths de hooks INTACTOS (shim de 17 líneas).
