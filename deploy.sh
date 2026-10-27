#!/bin/bash
# deploy.sh — deploy pipeline with a safety net.
#   ./deploy.sh             → tests → backup → copy → compile → ritual → reload gate
#   ./deploy.sh --rollback  → restores the last backup and restarts the poller
# Requires: bot idle for the reload (the gate only waits; it never cuts runs).
set -euo pipefail
cd "$(dirname "$0")"
HOOKS="$HOME/.zcode/hooks"
LABEL="com.zcode.telegram-cmds"
# Bot runtime: the 3.13 venv if present (bootstrap.sh), else the system one
PY="$HOOKS/venv/bin/python3"
[ -x "$PY" ] || PY=python3

if [ "${1:-}" = "--rollback" ]; then
  BK=$(ls -d "$HOOKS"/backups/*/ 2>/dev/null | tail -1)
  [ -n "$BK" ] || { echo "✗ no backups found"; exit 1; }
  echo "▶ restoring $BK"
  cp "$BK"zcode-telegram.py "$BK"zcode_proto.py "$HOOKS/" 2>/dev/null || true
  cp "$BK"nodock.sb "$HOOKS/" 2>/dev/null || true
  if [ -d "$BK/zcode_bot" ]; then
    rm -rf "$HOOKS/zcode_bot"
    cp -R "$BK/zcode_bot" "$HOOKS/zcode_bot"
  fi
  python3 -m py_compile "$HOOKS/zcode-telegram.py" "$HOOKS/zcode_proto.py"
  python3 -m compileall -q "$HOOKS/zcode_bot"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "✓ rollback applied and poller restarted"
  exit 0
fi

echo "▶ tests (runtime: $PY, parallel)"
"$PY" -m pytest -n auto -q
echo "✓ tests OK"

echo "▶ import gate (BEFORE copying — lesson from 15/10: the"
echo "  post-copy gate once left a non-importing version installed)"
"$PY" -m compileall -q src/zcode_bot src/zcode-telegram.py src/zcode_proto.py
(cd src && "$PY" -c "import zcode_bot.state, zcode_bot.concurrency, zcode_bot.proto, zcode_bot.cli_logs, zcode_bot.tasks_repo, zcode_bot.commands, zcode_bot.reports, zcode_bot.webhook_server, zcode_bot.gateway") \
  || { echo "✗ the zcode_bot package does not import"; exit 1; }
# Post-F4 ritual (ADR-012): anti-dup (one single live body per concept)
# + the structural checks (running_turns ×1 in core; gateway a pure
# facade) — both in scripts/, shared with CI.
"$PY" scripts/check_no_duplicates.py \
  || { echo "✗ anti-dup: duplicate symbol in src/zcode_bot (copied instead of moved?)"; exit 1; }
"$PY" scripts/selfcheck.py \
  || { echo "✗ selfcheck: the structural ritual failed (see above)"; exit 1; }
echo "✓ compiled + imports + anti-dup + ritual OK"

echo "▶ backup of the version in production"
BK="$HOOKS/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
for f in zcode-telegram.py zcode_proto.py nodock.sb; do
  [ -f "$HOOKS/$f" ] && cp "$HOOKS/$f" "$BK/"
done
[ -d "$HOOKS/zcode_bot" ] && cp -R "$HOOKS/zcode_bot" "$BK/"
echo "  $BK"

echo "▶ deploy src/ → hooks/"
cp src/zcode-telegram.py src/zcode_proto.py src/nodock.sb "$HOOKS/"
rm -f "$HOOKS/ctx_probe.py"  # removed in F31: nothing imports it
rm -rf "$HOOKS/zcode_bot"
cp -R src/zcode_bot "$HOOKS/zcode_bot"

echo "▶ bot-revive (F79d: the Mac-side red button — works when ALL is dead)"
mkdir -p "$HOME/.zcode/hooks/bin"
cp scripts/bot-revive.sh "$HOME/.zcode/hooks/bin/bot-revive"
chmod +x "$HOME/.zcode/hooks/bin/bot-revive"
echo "  installed: ~/.zcode/hooks/bin/bot-revive"
echo "  (optional, once: alias bot-revive='~/.zcode/hooks/bin/bot-revive')"

echo "▶ bot-healer agent (F79c: the healer of the healer — dumb, 60s, no network)"
PLIST_DST="$HOME/Library/LaunchAgents/com.zcode.bot-healer.plist"
sed -e "s|REPO|$(pwd)|" -e "s|HOME|$HOME|g" \
    scripts/com.zcode.bot-healer.plist > "$PLIST_DST"
if [ "${ZCODE_BOT_RUNTIME:-}" = "" ]; then
  # refresh an already-loaded agent (bootout is fine if it never loaded)
  launchctl bootout "gui/$(id -u)/com.zcode.bot-healer" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null \
    || echo "  (bot-healer will load at next login — bootstrap unavailable)"
fi
echo "  installed: $PLIST_DST"

echo "▶ reload through the door (instant since F16: in-flight turns"
echo "  are separate processes and do NOT depend on the poller)"
if [ -n "${ZCODE_BOT_RUNTIME:-}" ]; then
  # running INSIDE a bot runtime (phone turn): the sandbox (F14) blocks
  # launchctl and this turn's poller would die with us. Leave the door
  # armed and exit: the poller reloads on its next pass.
  touch "$HOOKS/state/reload-now"
  echo "▷ deploy from a headless turn: code copied and door armed."
  echo "  The poller will reload on its own (verify with /ping)."
  exit 0
fi
OLD=$(launchctl list | grep "$LABEL" | awk '{print $1}')
touch "$HOOKS/state/reload-now"
for i in $(seq 1 6); do
  sleep 5
  NEW=$(launchctl list | grep "$LABEL" | awk '{print $1}')
  if [ -n "$NEW" ] && [ "$NEW" != "-" ] && [ "$NEW" != "$OLD" ]; then
    echo "✓ new poller: PID $NEW (was $OLD)"
    echo "DONE. If something failed: ./deploy.sh --rollback"
    exit 0
  fi
done
echo "✗ the poller did not reload within 30 s — check ~/.zcode/hooks/logs/errors.log"
exit 1
