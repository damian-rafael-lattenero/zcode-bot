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
  python3 -m py_compile "$HOOKS/zcode-telegram.py" "$HOOKS/zcode_proto.py"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "✓ rollback aplicado y poller reiniciado"
  exit 0
fi

echo "▶ tests"
python3 -m unittest discover -s tests -q
echo "✓ tests OK"

echo "▶ backup de la versión en producción"
BK="$HOOKS/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
for f in zcode-telegram.py zcode_proto.py ctx_probe.py; do
  [ -f "$HOOKS/$f" ] && cp "$HOOKS/$f" "$BK/"
done
[ -d "$HOOKS/zcode_bot" ] && cp -R "$HOOKS/zcode_bot" "$BK/"
echo "  $BK"

echo "▶ deploy de src/ → hooks/"
cp src/zcode-telegram.py src/zcode_proto.py src/ctx_probe.py "$HOOKS/"
rm -rf "$HOOKS/zcode_bot"
cp -R src/zcode_bot "$HOOKS/zcode_bot"
python3 -m py_compile "$HOOKS/zcode-telegram.py" "$HOOKS/zcode_proto.py" "$HOOKS/zcode_bot/gateway.py"
(cd "$HOOKS" && python3 -c "import zcode_bot.state, zcode_bot.concurrency, zcode_bot.proto, zcode_bot.cli_logs, zcode_bot.tasks_repo, zcode_bot.commands, zcode_bot.reports, zcode_bot.gateway") \
  || { echo "✗ el paquete zcode_bot no importa"; exit 1; }
N=$(grep -c '^def running_turns' "$HOOKS/zcode_bot/gateway.py")
[ "$N" = "1" ] || { echo "✗ ritual: running_turns=$N (¿edición a medias?)"; exit 1; }
echo "✓ compiló + ritual OK"

echo "▶ recarga por la puerta (espera si hay corridas del bot en vuelo)"
OLD=$(launchctl list | grep "$LABEL" | awk '{print $1}')
touch "$HOOKS/state/reload-now"
for i in $(seq 1 60); do
  sleep 5
  NEW=$(launchctl list | grep "$LABEL" | awk '{print $1}')
  if [ -n "$NEW" ] && [ "$NEW" != "-" ] && [ "$NEW" != "$OLD" ]; then
    echo "✓ poller nuevo: PID $NEW (antes $OLD)"
    echo "LISTO. Si algo falló: ./deploy.sh --rollback"
    exit 0
  fi
done
echo "✗ el poller no se recargó en 5 min (¿corrida larga? — reintentá más tarde)"
exit 1
