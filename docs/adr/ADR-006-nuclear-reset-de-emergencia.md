# ADR-006 — /nuclear: reset de emergencia cerrando y reabriendo la app

**Estado**: aceptado (2026-10-12)
**Contexto**: /stop solo frena corridas iniciadas desde el celular (ADR-002:
un runtime no ve los turnos de otro). Las tasks corriendo en la UI de la Mac
no tienen freno externo — y en una emergencia (irse y dejar tokens
consumiéndose) hace falta cortar TODO desde el teléfono.

## Investigación (por qué NO hay ruta limpia)

- **Probe en vivo** (2026-10-12): los `zcode-cli` hijos de `zcode-host-local`
  exponen un UDS `znr-*.sock` cada uno; hablarle el protocolo `.`-framed con
  `session/list` no devuelve nada (0 bytes). El host solo tiene pipes
  anónimos con la app — sin listener externo.
- **zai-org/feedback#890** (investigación sobre Desktop 3.14.4): "The
  desktop's agent runtime communicates only over private stdio with the app;
  I found no secondary local listener for chat" — pide justo la feature que
  necesitaríamos (ingress local a sesión abierta de la app). Hoy no existe.
- **FAQ oficial** (zcode.z.ai/en/docs/qa): "With ZCode fully quit and no
  background commands running, configuration and session records are
  unaffected" — cerrar la app por completo es SEGURO para config y sesiones.

## Decisión

`/nuclear` (alias `/nuke`), confirmación en DOS pasos (solo el botón produce
`go`; tipear "nuclear" solo muestra la confirmación):

1. `session/stop` a cada worker del bot (ADR-002: mismo runtime, stop real).
2. SIGTERM a los runtimes app-server hijos del poller (garantía aunque algún
   stop falle; los workers lo ven como "app-server murió").
3. `killall -TERM ZCode` (cierre normal, SIN permisos de osascript) → si no
   cierra en 20 s, SIGKILL de rescate. Todo lo de la UI muere acá.
4. El host huérfano se termina SOLO si la app ya murió (única excepción a la
   regla "nunca matar zcode-host-local": la regla protege el caso app-viva).
5. `open -a ZCode` (reinicio) + verificación de proceso (40 s).
6. `launchctl kickstart -k` del bot. Seguro: `save_offset` precede al
   dispatch del comando → el /nuclear no se reprocesa al arrancar.

## Consecuencias

- El bot NO depende de la app (poller LaunchAgent + runtimes propios):
  sobrevive al ciclo y es el que reporta el resultado.
- Estado no guardado en editores de la app se pierde (aceptado: es
  emergencia; la doc oficial dice que config/sesiones no se afectan).
- Si aparece un día el ingress local (feedback#890), el paso 3-5 se
  reemplaza por `session/stop` contra el runtime de la app: el comando
  ya está aislado en `app_restart_cycle`.
