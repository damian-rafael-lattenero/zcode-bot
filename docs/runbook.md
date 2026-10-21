# Runbook — operation and incidents

## Update transport (ADR-011, since F3): polling | webhook
- **polling** (default): 3s long-poll by the async poller. Zero infra.
- **webhook**: Telegram pushes to `https://<host>:8443/telegram` with a
  secret (`X-Telegram-Bot-Api-Secret-Token`, fail-closed in the receiver).
  Requires: 1) cert.pem/key.pem in ~/.zcode/hooks (self-signed is fine),
  2) a reachable public URL (port-forward of 8443 or a tunnel),
  3) `/set_webhook <url>` from the chat — generates the secret, saves it
  and sets `transport=webhook`; the poller picks it up on the next start.
- **Back to polling**: `/delete_webhook` (or set `"transport": "polling"`
  in config and reload). Never both at once: Telegram 409.
- Diagnostics: `/webhook_info` (pending_update_count, last_error_date),
  `GET https://<host>:8443/health` (local, no auth).

## Runtime (since 2026-10-15): Python 3.13 + its own venv
- **Interpreter**: `~/.zcode/hooks/venv/bin/python3` — Python 3.13 (uv-managed
  standalone arm64 build, `~/.local/share/uv/python/`). The system 3.9 went
  EOL in Oct-2025 and is no longer used for the bot.
- **Prepare/repair the runtime**: `./bootstrap.sh` (idempotent: installs
  uv if missing, Python 3.13, the venv and the pinned `requirements.txt`).
- **Dependencies**: `requirements.txt` with exact pins (aiohttp 3.14.5);
  `deploy.sh` uses the venv automatically if it exists.
- **LaunchAgents**: `com.zcode.telegram-cmds` (watch-commands) and
  `com.zcode.telegram-watch` (watch-app) point at the venv. Switching
  interpreter requires reloading the definitions:
  `launchctl bootout gui/$(id -u)/<label> && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`
- **Runtime rollback**: edit the plists back to `/usr/bin/python3` and
  reload (the code is still 3.9-compatible).

## Architecture (since F16, 2026-10-13): the ACTOR model
- **The poller** (`watch-commands`) is ONLY a supervisor: getUpdates,
  commands, acks. Restarting it (kickstart, deploy, crash) does NOT touch
  in-flight turns.
- **Each worker is a detached PROCESS** (`--run-turn`, start_new_session):
  owner of its runtime, its messages and its lifecycle. It reports on its
  own through Telegram. Registry in `state/bot-workers.json` (flock, with
  pid).
- **All communication goes through files** (mailboxes in state/), and the
  waits are REACTIVE (`fswatch`: kqueue sleeps in the kernel, wakes on the
  event — no polling loops):
  - `bot-workers.json` — registry {sid: {pid, t0, prompt, ws, title,
    stopped}}; automatic pruning of dead pids (max age 4h against pid
    reuse).
  - `bot-stop/<sid>` — the /stop order: the poller writes it, the worker
    sees it (~2 s) and runs session/stop on ITS OWN runtime.
  - `bot-q/` — questions ❓: `q-<rid>.json` (worker asks) /
    `a-<rid>.json` (poller answers when the button is tapped).
  - `bot-queue.json` — prompt queue (flock). It sleeps while the task is
    busy or no longer the current one; it wakes up on its own when the
    turn ends (on_stop) or when you come back with /task.
  - `bot-nuclear` — nuclear-cycle flag (the reload gate respects it).
- **Only-current-task filter (F15)**: unsolicited push ONLY for the active
  task (`only_active_task` in config = kill switch). Sessions with a
  worker don't duplicate hooks (▶️/✅). The turn's final reply ALWAYS
  arrives. `/stop` halts AND clears that task's queue (F17).

## Deploy and rollback
- `./deploy.sh` — the whole pipeline; INSTANT reload (30 s max): in-flight
  turns are separate processes and keep running the old code until they
  finish (the file contracts tolerate that).
- `./deploy.sh --rollback` — restores the last backup
  (`~/.zcode/hooks/backups/`).
- Manual door: `touch ~/.zcode/hooks/state/reload-now` (the poller exits
  on its next pass; launchd relaunches it).

## Processes (what each one is and what NOT to touch)
- `zcode-telegram.py watch-commands` — the poller (cmds LaunchAgent).
- `zcode-telegram.py watch-app` — app watcher (watch LaunchAgent).
- `zcode-telegram.py hook` — short-lived processes, one per ZCode event.
- `zcode-telegram.py --stuck-watch` — subprocess per turn (reports stuck
  ones).
- `zcode-telegram.py --run-turn` — THE WORKERS: processes independent of
  the poller. Restarting the poller does not kill them. To stop one:
  /stop (marker) or, in an emergency, SIGTERM to the pid from the
  registry (they shut down cleanly).
- `zcode-host-local-*` — the APP's runtimes. **NEVER kill them.**
- `ZCode` app + its `zcode-cli` children — do not touch.

## Known incidents

### Bot silent / not reacting
1. `launchctl list | grep telegram-cmds` — is there a PID? exit code ≠ 0?
2. `tail ~/.zcode/hooks/logs/watch-cmds.log` — any traceback?
3. Stuck run? `tail ~/.zcode/hooks/logs/prompt-worker.log`
   (each worker logs with its pid) and `cat state/bot-workers.json`.
4. Poller restart: ALWAYS safe for turns (actors).

### Compact "absorbed" by the app
Rule: compact from where the task is OPEN (ADR-003). Authoritative
numbers: the `compaction` part (pre → truePost).

### /stop doesn't halt something running in the app
By design (ADR-002): phone-only runs. Use the Stop button on the Mac. If
it's an emergency and you are not at the Mac: `/nuclear` (see below).

### /nuclear (emergency reset, ADR-006)
Halts EVERYTHING (bot runs + UI tasks) and restarts app and bot.
Two-step confirmation: `/nuclear` → the ☢️ button.
- What it does: /stop marker to each worker (F16) → SIGTERM to the pids
  in the registry (5 s grace, SIGKILL for survivors) →
  `killall -TERM ZCode` (rescue SIGKILL if it doesn't close) → orphan
  host only if the app is already dead → `open -a ZCode` → bot kickstart
  (offset already committed: nothing is reprocessed).
- No osascript permissions needed: killall/open/launchctl prompt nothing.
- The bot does NOT depend on the app: it keeps reporting through the
  whole cycle.
- If the app doesn't come back within 40 s, the final message says so —
  open it by hand.
- Full cycle log: `~/.zcode/hooks/logs/prompt-worker.log` ("NUCLEAR2").

### Network down
Messages fall into the outbox (multi-process flock) and resend on their
own.

## Config
- `~/.zcode/telegram-remote.json` — bot token/chat (chmod 600).
- `~/.zcode/hooks/bot-provider.json` — bot API key (NEVER log it).
- Hooks in `~/.zcode/cli/config.json` (7 events, official guide).
- `/etc/hosts`: fix `149.154.166.110 api.telegram.org` + IPv6 Off (if the
  bot answers from the phone, the network is fine — don't over-check).
