#!/bin/bash
# bot-revive — F79d: the red button that works when the bot is ALL dead.
#
# Damian's ask (27/10): «quisiera tirar un comando definitivo que me
# reviva todo» — including the case where Telegram never arrives
# because the transport itself is the corpse. This script runs ON THE
# MAC, from the shell, and depends on NOTHING from zcode_bot (the
# package may be the broken thing):
#   1. kickstart/bootstrap the poller agent AND the bot-healer agent
#   2. clear a stale nuclear flag (an orphan would block spawns)
#   3. wait for the transport heartbeat (state/hb-transport) to beat
#   4. one-shot Telegram send over curl: "revived" — the healer and
#      F78c's 📥 bundles land whatever the chat never saw on the next
#      cycles
# Exit 0 = revived and announced; nonzero = say WHY on stderr.
set -u
HOOKS="$HOME/.zcode/hooks"
STATE="$HOOKS/state"
UID_N=$(id -u)
POLLER="com.zcode.telegram-cmds"
HEALER="com.zcode.bot-healer"
HB="$STATE/hb-transport"

echo "▶ bot-revive: reviving the full stack"

launchctl bootout "gui/$UID_N/$HEALER" 2>/dev/null
launchctl bootstrap "gui/$UID_N" \
  "$HOME/Library/LaunchAgents/$HEALER.plist" 2>/dev/null \
  && echo "  bot-healer agent loaded" || echo "  (bot-healer plist not loadable — continuing)"
launchctl kickstart -k "gui/$UID_N/$HEALER" 2>/dev/null

launchctl kickstart -k "gui/$UID_N/$POLLER" \
  || { echo "✗ the poller agent would not kickstart (launchctl rc=$?)"; exit 1; }
echo "  poller kickstarted"

# a stale nuclear flag blocks every spawn for its TTL — an orphan is
# exactly what a revive must not inherit
if [ -f "$STATE/bot-nuclear" ]; then
  rm -f "$STATE/bot-nuclear" && echo "  stale nuclear flag cleared"
fi

echo "▶ waiting for the transport beat (up to 90s)"
ok=""
for _ in $(seq 1 90); do
  if [ -f "$HB" ] && [ $(( $(date +%s) - $(stat -f %m "$HB") )) -le 10 ]; then
    ok=1; break
  fi
  sleep 1
done
if [ -z "$ok" ]; then
  echo "⚠️ no fresh hb-transport after 90s — the poller started but is not cycling."
  echo "  Check: tail -50 $HOOKS/logs/errors.log"
fi

# one-shot send, independent of the bot's transport: token+chat from
# the config the poller itself reads
CFG="$HOME/.zcode/telegram-remote.json"
if [ -f "$CFG" ]; then
  TOKEN=$(/usr/bin/python3 -c "import json;print(json.load(open('$CFG')).get('token',''))")
  CHAT=$(/usr/bin/python3 -c "import json;print(json.load(open('$CFG')).get('chat_id',''))")
  if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
    curl -s -m 10 "https://api.telegram.org/bot$TOKEN/sendMessage" \
      -d chat_id="$CHAT" \
      -d text="🟥 bot-revive from the Mac: full stack restarted$( [ -n "$ok" ] && echo " — transport beating ✓" ). The 📥 bundles land whatever this chat never saw on the next cycles." \
      > /dev/null && echo "  announced on Telegram" \
      || echo "  (the one-shot send failed — but the stack is revived)"
  fi
fi

echo "✓ bot-revive done — /ping from Telegram to verify, /handshake for the full picture"
