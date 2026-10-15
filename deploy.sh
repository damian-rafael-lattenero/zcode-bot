#!/bin/bash
# deploy.sh — pipeline de deploy con red de seguridad.
#   ./deploy.sh             → tests → backup → copia → compila → ritual → reload gate
#   ./deploy.sh --rollback  → restaura el último backup y reinicia el poller
# Requiere: bot idle para la recarga (el gate espera solo; no corta corridas).
set -euo pipefail
cd "$(dirname "$0")"
HOOKS="$HOME/.zcode/hooks"
LABEL="com.zcode.telegram-cmds"

if [ "${1:-}" = "--rollback" ]; then
  BK=$(ls -d "$HOOKS"/backups/*/ 2>/dev/null | tail -1)
  [ -n "$BK" ] || { echo "✗ no hay backups"; exit 1; }
  echo "▶ restaurando $BK"
  cp "$BK"zcode-telegram.py "$BK"zcode_proto.py "$HOOKS/" 2>/dev/null || true
  cp "$BK"ctx_probe.py "$HOOKS/" 2>/dev/null || true
  cp "$BK"nodock.sb "$HOOKS/" 2>/dev/null || true
  python3 -m py_compile "$HOOKS/zcode-telegram.py" "$HOOKS/zcode_proto.py"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "✓ rollback aplicado y poller reiniciado"
  exit 0
fi

echo "▶ tests"
python3 -m unittest discover -s tests -q
echo "✓ tests OK"

echo "▶ gate de import (ANTES de copiar — lección del 15/10: el gate"
echo "  posterior al cp dejó una versión que no importa instalada)"
python3 -m compileall -q src/zcode_bot src/zcode-telegram.py src/zcode_proto.py
(cd src && python3 -c "import zcode_bot.state, zcode_bot.concurrency, zcode_bot.proto, zcode_bot.cli_logs, zcode_bot.tasks_repo, zcode_bot.commands, zcode_bot.reports, zcode_bot.webhook_server, zcode_bot.gateway") \
  || { echo "✗ el paquete zcode_bot no importa"; exit 1; }
N=$(grep -c '^def running_turns' src/zcode_bot/gateway.py)
[ "$N" = "1" ] || { echo "✗ ritual: running_turns=$N (¿edición a medias?)"; exit 1; }
echo "✓ compiló + importa + ritual OK"

echo "▶ backup de la versión en producción"
BK="$HOOKS/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
for f in zcode-telegram.py zcode_proto.py ctx_probe.py nodock.sb; do
  [ -f "$HOOKS/$f" ] && cp "$HOOKS/$f" "$BK/"
done
[ -d "$HOOKS/zcode_bot" ] && cp -R "$HOOKS/zcode_bot" "$BK/"
echo "  $BK"

echo "▶ deploy de src/ → hooks/"
cp src/zcode-telegram.py src/zcode_proto.py src/ctx_probe.py src/nodock.sb "$HOOKS/"
rm -rf "$HOOKS/zcode_bot"
cp -R src/zcode_bot "$HOOKS/zcode_bot"

echo "▶ recarga por la puerta (instantánea desde F16: los turnos en vuelo"
echo "  son procesos separados y NO dependen del poller)"
if [ -n "${ZCODE_BOT_RUNTIME:-}" ]; then
  # corriendo DENTRO de un runtime del bot (turno del celular): el sandbox
  # (F14) bloquea launchctl y el poller de este turno moriría con nosotros.
  # Dejar la puerta armada y salir: el poller recarga en su próxima vuelta.
  touch "$HOOKS/state/reload-now"
  echo "▷ deploy desde turno headless: código copiado y puerta armada."
  echo "  El poller recargará solo (verificar con /ping)."
  exit 0
fi
OLD=$(launchctl list | grep "$LABEL" | awk '{print $1}')
touch "$HOOKS/state/reload-now"
for i in $(seq 1 6); do
  sleep 5
  NEW=$(launchctl list | grep "$LABEL" | awk '{print $1}')
  if [ -n "$NEW" ] && [ "$NEW" != "-" ] && [ "$NEW" != "$OLD" ]; then
    echo "✓ poller nuevo: PID $NEW (antes $OLD)"
    echo "LISTO. Si algo falló: ./deploy.sh --rollback"
    exit 0
  fi
done
echo "✗ el poller no se recargó en 30 s — mirar ~/.zcode/hooks/logs/errors.log"
exit 1
