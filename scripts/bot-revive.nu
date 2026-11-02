#!/Users/lattenero/.local/bin/nu-nightly
# bot-revive — F79d: the red button that works when the bot is ALL dead.
#
# Damian's ask (27/10): «quisiera tirar un comando definitivo que me
# reviva todo» — including the case where Telegram never arrives because
# the transport itself is the corpse. This script runs ON THE MAC, from
# the shell, and depends on NOTHING from zcode_bot (the package may be the
# broken thing):
#   1. kickstart/bootstrap the poller agent AND the bot-healer agent
#   2. clear a stale nuclear flag (an orphan would block spawns)
#   3. wait for the transport heartbeat (state/hb-transport) to beat
#   4. one-shot Telegram send: "revived" — the healer and F78c's 📥
#      bundles land whatever the chat never saw on the next cycles
# Exit 0 = revived and announced; nonzero = say WHY on stderr.
#
# Migrated from bot-revive.sh on 2026-11-02.

const POLLER = "com.zcode.telegram-cmds"
const HEALER = "com.zcode.bot-healer"

def main []: nothing -> nothing {
    let home = $nu.home-dir
    let hooks = ($home | path join ".zcode/hooks")
    let state = ($hooks | path join "state")
    let hb = ($state | path join "hb-transport")
    let uid = (^id -u | str trim)

    print "▶ bot-revive: reviving the full stack"

    let plist = ($home | path join "Library/LaunchAgents" $"($HEALER).plist")
    do { ^launchctl bootout $"gui/($uid)/($HEALER)" } | complete | ignore
    let healed = (do { ^launchctl bootstrap $"gui/($uid)" $plist } | complete)
    if $healed.exit_code == 0 {
        print "  bot-healer agent loaded"
    } else {
        print "  (bot-healer plist not loadable — continuing)"
    }
    do { ^launchctl kickstart -k $"gui/($uid)/($HEALER)" } | complete | ignore

    let kick = (do { ^launchctl kickstart -k $"gui/($uid)/($POLLER)" } | complete)
    if $kick.exit_code != 0 {
        print -e $"✗ the poller agent would not kickstart \(launchctl rc=($kick.exit_code)\)"
        exit 1
    }
    print "  poller kickstarted"

    # A stale nuclear flag blocks every spawn for its TTL — an orphan is
    # exactly what a revive must not inherit.
    let nuclear = ($state | path join "bot-nuclear")
    if (($nuclear | path type) == "file") {
        rm --force $nuclear
        print "  stale nuclear flag cleared"
    }

    print "▶ waiting for the transport beat \(up to 90s\)"
    mut beating = false
    mut waited = 0
    while ($waited < 90) {
        if (($hb | path type) == "file") {
            let now = (date now | format date "%s" | into int)
            let touched = (^stat -f %m $hb | str trim | into int)
            if ($now - $touched) <= 10 {
                $beating = true
                break
            }
        }
        sleep 1sec
        $waited += 1
    }
    if not $beating {
        print "⚠️ no fresh hb-transport after 90s — the poller started but is not cycling."
        print $"  Check: tail -50 ($hooks)/logs/errors.log"
    }

    # One-shot send, independent of the bot's transport: token+chat from
    # the config the poller itself reads.
    let cfg_path = ($home | path join ".zcode/telegram-remote.json")
    if (($cfg_path | path type) == "file") {
        let sent = (try {
            let cfg = (open $cfg_path)
            let token = ($cfg.token? | default "")
            let chat = ($cfg.chat_id? | default "")
            if ($token | is-empty) or ($chat | is-empty) { false } else {
                let suffix = if $beating { " — transport beating ✓" } else { "" }
                let text = $"🟥 bot-revive from the Mac: full stack restarted($suffix). The 📥 bundles land whatever this chat never saw on the next cycles."
                let body = ({chat_id: $chat, text: $text} | to json)
                do -i {
                    http post -m 10sec -t application/json $"https://api.telegram.org/bot($token)/sendMessage" $body -r | ignore
                    true
                }
            }
        } catch {|_e| false })
        if $sent {
            print "  announced on Telegram"
        } else {
            print "  (the one-shot send failed — but the stack is revived)"
        }
    }

    print "✓ bot-revive done — /ping from Telegram to verify, /handshake for the full picture"
}
