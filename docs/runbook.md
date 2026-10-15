# Runbook — operación e incidentes

## Runtime (desde 2026-10-15): Python 3.13 + venv propio
- **Intérprete**: `~/.zcode/hooks/venv/bin/python3` — Python 3.13 (build
  standalone arm64 gestionado por uv, `~/.local/share/uv/python/`). El
  3.9 del sistema quedó EOL en oct-2025 y ya no se usa para el bot.
- **Preparar/reparar el runtime**: `./bootstrap.sh` (idempotente: instala
  uv si falta, Python 3.13, venv y `requirements.txt` pinneado).
- **Dependencias**: `requirements.txt` con pin exacto (aiohttp 3.14.5);
  `deploy.sh` usa el venv automáticamente si existe.
- **LaunchAgents**: `com.zcode.telegram-cmds` (watch-commands) y
  `com.zcode.telegram-watch` (watch-app) apuntan al venv. Cambiar de
  intérprete requiere recargar las definiciones:
  `launchctl bootout gui/$(id -u)/<label> && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`
- **Rollback de runtime**: editar los plists de vuelta a
  `/usr/bin/python3` y recargar (el código sigue siendo compatible 3.9).

## Arquitectura (desde F16, 2026-10-13): modelo de ACTORES
- **El poller** (`watch-commands`) es SOLO supervisor: getUpdates, comandos,
  acks. Reiniciarlo (kickstart, deploy, crash) **no toca los turnos**.
- **Cada worker es un PROCESO detached** (`--run-turn`, start_new_session):
  dueño de su runtime, de sus mensajes y de su ciclo de vida. Se reporta
  solo por Telegram. Registry en `state/bot-workers.json` (flock, con pid).
- **Toda comunicación es por archivos** (mailboxes de state/), y las
  esperas son REACTIVAS (`fswatch`: kqueue duerme en el kernel, despierta
  con el evento — sin sondeos):
  - `bot-workers.json` — registry {sid: {pid, t0, prompt, ws, titulo,
    stopped}}; poda automática de pids muertos (edad máx 4h anti reuse).
  - `bot-stop/<sid>` — orden de /stop: la escribe el poller, el worker la
    ve (~2 s) y ejecuta session/stop sobre SU runtime.
  - `bot-q/` — preguntas ❓: `q-<rid>.json` (worker pregunta) /
    `a-<rid>.json` (poller responde al tocar el botón).
  - `bot-queue.json` — cola de prompts (flock). Duerme si la task está
    ocupada o dejó de ser la current; despierta sola al terminar el turno
    (on_stop) o al volver con /task.
  - `bot-nuclear` — flag del ciclo nuclear (el gate de reload lo respeta).
- **Filtro solo-task-current (F15)**: push no solicitado SOLO de la task
  activa (`only_active_task` en config = kill switch). Sesiones con
  worker no duplican hooks (▶️/✅). La respuesta final del turno SIEMPRE
  llega. `/stop` frena Y limpia la cola de esa task (F17).

## Deploy y rollback
- `./deploy.sh` — todo el pipeline; recarga INSTANTÁNEA (30 s máx): los
  turnos en vuelo son procesos separados y siguen con código viejo hasta
  terminar (los contratos de archivo lo toleran).
- `./deploy.sh --rollback` — restaura el último backup (`~/.zcode/hooks/backups/`).
- Puerta manual: `touch ~/.zcode/hooks/state/reload-now` (el poller sale
  en su próxima vuelta; launchd lo relanza).

## Procesos (qué es cada uno y qué NO tocar)
- `zcode-telegram.py watch-commands` — el poller (LaunchAgent cmds).
- `zcode-telegram.py watch-app` — vigilante de la app (LaunchAgent watch).
- `zcode-telegram.py hook` — procesos cortos por evento de ZCode.
- `zcode-telegram.py --stuck-watch` — subproceso por turno (avisa trabadas).
- `zcode-telegram.py --run-turn` — LOS WORKERS: procesos independientes del
  poller. Reiniciar el poller no los mata. Para frenar uno: /stop (marker)
  o, en emergencia, SIGTERM al pid del registry (cierran limpio).
- `zcode-host-local-*` — runtimes de la APP. **NUNCA matarlos.**
- `ZCode` app + sus `zcode-cli` hijos — no tocar.

## Incidentes conocidos

### Bot mudo / no reacciona
1. `launchctl list | grep telegram-cmds` — ¿PID presente? ¿exit code ≠ 0?
2. `tail ~/.zcode/hooks/logs/watch-cmds.log` — ¿traceback?
3. ¿Corrida colgada? `tail ~/.zcode/hooks/logs/prompt-worker.log`
   (cada worker loguea con su pid) y `cat state/bot-workers.json`.
4. Reinicio del poller: SIEMPRE seguro para los turnos (actores).

### Compact "absorbido" por la app
Regla: compactar desde donde la task está ABIERTA (ADR-003). Números
autoritativos: part `compaction` (pre → truePost).

### /stop no frena algo que corre en la app
Por diseño (ADR-002): solo-corridas-del-celular. Botón Stop en la Mac.
Si es emergencia y no estás en la Mac: `/nuclear` (ver abajo).

### /nuclear (reset de emergencia, ADR-006)
Frena TODO (corridas del bot + tasks de la UI) y reinicia app y bot.
Confirmación en dos pasos: `/nuclear` → botón ☢️.
- Qué hace: marker de /stop a cada worker (F16) → SIGTERM a los pids del
  registry (grace 5 s, SIGKILL al sobreviviente) → `killall -TERM ZCode`
  (SIGKILL de rescate si no cierra) → host huérfano solo si la app ya
  murió → `open -a ZCode` → kickstart del bot (offset ya committeado:
  no se reprocesa).
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
