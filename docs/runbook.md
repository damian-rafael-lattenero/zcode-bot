# Runbook — operación e incidentes

## Deploy y rollback
- `./deploy.sh` — todo el pipeline; espera solo si hay corridas del bot.
- `./deploy.sh --rollback` — restaura el último backup (`~/.zcode/hooks/backups/`).
- Puerta manual: `touch ~/.zcode/hooks/state/reload-now` (el poller sale
  solo cuando no hay corridas en vuelo; launchd lo relanza).

## Procesos (qué es cada uno y qué NO tocar)
- `zcode-telegram.py watch-commands` — el poller (LaunchAgent cmds).
- `zcode-telegram.py watch-app` — vigilante de la app (LaunchAgent watch).
- `zcode-telegram.py hook` — procesos cortos por evento de ZCode.
- `zcode-telegram.py --stuck-watch` — subproceso por turno (avisa trabadas).
- `zcode-host-local-*` — runtimes de la APP. **NUNCA matarlos.**
- `ZCode` app + sus `zcode-cli` hijos — no tocar.

## Incidentes conocidos

### Bot mudo / no reacciona
1. `launchctl list | grep telegram-cmds` — ¿PID presente? ¿exit code ≠ 0?
2. `tail ~/.zcode/hooks/logs/watch-cmds.log` — ¿traceback?
3. ¿Corrida colgada? `tail ~/.zcode/hooks/logs/prompt-worker.log`.
4. Causa histórica: deadlock por Lock no reentrante (12/10, ADR-002;
   hoy testeado como regresión). Síntoma: proceso "vivo" al 0% CPU,
   runtime hijo colgado >5 min.
5. Reinicio seguro: `launchctl kickstart -k gui/$(id -u)/com.zcode.telegram-cmds`
   (SOLO si no hay corridas en vuelo que importen).

### Compact "absorbido" por la app
Regla: compactar desde donde la task está ABIERTA (ADR-003). Números
autoritativos: part `compaction` (pre → truePost).

### /stop no frena algo que corre en la app
Por diseño (ADR-002): solo-corridas-del-celular. Botón Stop en la Mac.
Si es emergencia y no estás en la Mac: `/nuclear` (ver abajo).

### /nuclear (reset de emergencia, ADR-006)
Frena TODO (corridas del bot + tasks de la UI) y reinicia app y bot.
Confirmación en dos pasos: `/nuclear` → botón ☢️.
- Qué hace: session/stop a cada worker → SIGTERM a los app-server hijos
  del poller → `killall -TERM ZCode` (SIGKILL de rescate si no cierra) →
  host huérfano solo si la app ya murió → `open -a ZCode` → kickstart
  del bot (offset ya committeado: no se reprocesa).
- Sin permisos de osascript: killall/open/launchctl no piden nada.
- El bot NO depende de la app: sigue reportando durante todo el ciclo.
- Si la app no vuelve en 40 s, lo dice en el mensaje final — abrir a mano.
- Log completo del ciclo: `~/.zcode/hooks/logs/prompt-worker.log` ("NUCLEAR").

### Red caída
Los mensajes caen al outbox (flock multi-proceso) y se reenvían solos.

## Config
- `~/.zcode/telegram-remote.json` — token/chat del bot (chmod 600).
- `~/.zcode/hooks/bot-provider.json` — API key del bot (NUNCA loguear).
- Hooks en `~/.zcode/cli/config.json` (7 eventos, guía oficial).
- `/etc/hosts`: fix `149.154.166.110 api.telegram.org` + IPv6 Off (si el
  bot responde desde el móvil, la red está bien — no revisar de más).
