#!/Users/lattenero/.local/bin/nu-nightly
# deploy.nu — deploy pipeline with a safety net.
#   ./deploy.nu             → tests → backup → copy → compile → ritual → reload gate
#   ./deploy.nu --rollback  → restores the last backup and restarts the poller
# Requires: bot idle for the reload (the gate only waits; it never cuts runs).
#
# Migrated from deploy.sh on 2026-11-02. The bash helper functions and the
# `launchctl list | grep | awk` become typed values.
#
# NOTE (2026-11-02): the bot is retired in favour of the Kotlin daemon.
# This pipeline is kept green as the reference implementation and the
# rollback door; it is NOT part of the daily deploy anymore.

const LABEL = "com.zcode.telegram-cmds"

def pid-of [label: string]: nothing -> string {
    let rows = (^launchctl list | lines | where {|l| $l | str contains $label })
    if ($rows | is-empty) { return "" }
    let cols = ($rows | first | str trim | split row "\t")
    ($cols | first | str trim)
}

def main [--rollback]: nothing -> nothing {
    let repo = $env.FILE_PWD
    cd $repo
    let home = $nu.home-dir
    let uid = (^id -u | str trim)
    let hooks = ($home | path join ".zcode/hooks")
    let venv_py = ($hooks | path join "venv/bin/python3")
    # Bot runtime: the 3.13 venv if present (bootstrap.nu), else the system one.
    let py = if ((do { ^test -x $venv_py } | complete).exit_code == 0) { $venv_py } else { "python3" }

    if $rollback {
        let backups = (ls ($hooks | path join "backups") | where type == dir | sort-by modified -r)
        if ($backups | is-empty) {
            print -e "✗ no backups found"
            exit 1
        }
        let bk = (($backups | first).name)
        print $"▶ restoring ($bk)"
        for name in ["zcode-telegram.py" "zcode_proto.py" "nodock.sb"] {
            let src = ($bk | path join $name)
            if (($src | path type) == "file") {
                ^cp -f $src $hooks
            }
        }
        let old_pkg = ($bk | path join "zcode_bot")
        if (($old_pkg | path type) == "dir") {
            rm -rf ($hooks | path join "zcode_bot")
            ^cp -R $old_pkg ($hooks | path join "zcode_bot")
        }
        ^$py -m py_compile ($hooks | path join "zcode-telegram.py") ($hooks | path join "zcode_proto.py")
        ^$py -m compileall -q ($hooks | path join "zcode_bot")
        ^launchctl kickstart -k $"gui/($uid)/($LABEL)"
        print "✓ rollback applied and poller restarted"
        return
    }

    print $"▶ tests \(runtime: ($py), parallel\)"
    ^$py -m pytest -n auto -q
    print "✓ tests OK"

    print "▶ import gate \(BEFORE copying — lesson from 15/10: the"
    print "  post-copy gate once left a non-importing version installed\)"
    ^$py -m compileall -q src/zcode_bot src/zcode-telegram.py src/zcode_proto.py
    let imports = (do {
        cd ($repo | path join "src")
        ^$py -c "import zcode_bot.state, zcode_bot.concurrency, zcode_bot.proto, zcode_bot.cli_logs, zcode_bot.tasks_repo, zcode_bot.commands, zcode_bot.reports, zcode_bot.webhook_server, zcode_bot.gateway"
    } | complete)
    if $imports.exit_code != 0 {
        print -e "✗ the zcode_bot package does not import"
        print -e ($imports.stderr | str trim)
        exit 1
    }
    # Post-F4 ritual (ADR-012): anti-dup (one single live body per concept)
    # + the structural checks (running_turns ×1 in core; gateway a pure
    # facade) — both in scripts/, shared with CI.
    let dup = (do { ^$py scripts/check_no_duplicates.py } | complete)
    if $dup.exit_code != 0 {
        print -e "✗ anti-dup: duplicate symbol in src/zcode_bot \(copied instead of moved?\)"
        print -e ($dup.stderr | str trim)
        exit 1
    }
    let self = (do { ^$py scripts/selfcheck.py } | complete)
    if $self.exit_code != 0 {
        print -e "✗ selfcheck: the structural ritual failed \(see above\)"
        print -e ($self.stderr | str trim)
        exit 1
    }
    print "✓ compiled + imports + anti-dup + ritual OK"

    print "▶ backup of the version in production"
    let bk = ($hooks | path join "backups" (date now | format date "%Y%m%d-%H%M%S"))
    mkdir $bk
    for name in ["zcode-telegram.py" "zcode_proto.py" "nodock.sb"] {
        let src = ($hooks | path join $name)
        if (($src | path type) == "file") {
            ^cp -f $src $bk
        }
    }
    let live_pkg = ($hooks | path join "zcode_bot")
    if (($live_pkg | path type) == "dir") {
        ^cp -R $live_pkg ($bk | path join "zcode_bot")
    }
    print $"  ($bk)"

    print "▶ deploy src/ → hooks/"
    ^cp -f src/zcode-telegram.py src/zcode_proto.py src/nodock.sb $hooks
    rm --force ($hooks | path join "ctx_probe.py")  # removed in F31: nothing imports it
    rm -rf ($hooks | path join "zcode_bot")
    ^cp -R src/zcode_bot ($hooks | path join "zcode_bot")

    print "▶ bot-revive \(F79d: the Mac-side red button — works when ALL is dead\)"
    mkdir ($hooks | path join "bin")
    ^cp -f scripts/bot-revive.nu ($hooks | path join "bin/bot-revive")
    ^chmod +x ($hooks | path join "bin/bot-revive")
    print "  installed: ~/.zcode/hooks/bin/bot-revive"
    print "  (optional, once: alias bot-revive='~/.zcode/hooks/bin/bot-revive')"

    print "▶ bot-healer agent \(F79c: the healer of the healer — dumb, 60s, no network\)"
    let plist_src = ($repo | path join "scripts/com.zcode.bot-healer.plist")
    let plist_dst = ($home | path join "Library/LaunchAgents/com.zcode.bot-healer.plist")
    let rendered = (open --raw $plist_src
        | str replace --all "REPO" $repo
        | str replace --all "HOME" $home)
    $rendered | save -f $plist_dst
    if ($env.ZCODE_BOT_RUNTIME? | is-empty) {
        # Refresh an already-loaded agent (bootout is fine if it never loaded).
        do { ^launchctl bootout $"gui/($uid)/com.zcode.bot-healer" } | complete | ignore
        let boot = (do { ^launchctl bootstrap $"gui/($uid)" $plist_dst } | complete)
        if $boot.exit_code != 0 {
            print "  (bot-healer will load at next login — bootstrap unavailable)"
        }
    }
    print $"  installed: ($plist_dst)"

    print "▶ reload through the door \(instant since F16: in-flight turns"
    print "  are separate processes and do NOT depend on the poller\)"
    if not ($env.ZCODE_BOT_RUNTIME? | is-empty) {
        # Running INSIDE a bot runtime (phone turn): the sandbox (F14) blocks
        # launchctl and this turn's poller would die with us. Leave the door
        # armed and exit: the poller reloads on its next pass.
        ^touch ($hooks | path join "state/reload-now")
        print "▷ deploy from a headless turn: code copied and door armed."
        print "  The poller will reload on its own \(verify with /ping\)."
        return
    }
    let old = (pid-of $LABEL)
    ^touch ($hooks | path join "state/reload-now")
    mut i = 0
    while ($i < 6) {
        sleep 5sec
        let new = (pid-of $LABEL)
        if (not ($new | is-empty)) and ($new != "-") and ($new != $old) {
            print $"✓ new poller: PID ($new) \(was ($old)\)"
            print "DONE. If something failed: ./deploy.nu --rollback"
            return
        }
        $i += 1
    }
    print -e "✗ the poller did not reload within 30 s — check ~/.zcode/hooks/logs/errors.log"
    exit 1
}
