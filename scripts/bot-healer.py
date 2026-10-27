#!/usr/bin/env python3
"""bot-healer — the healer of the healer (F79c).

A DELIBERATELY dumb launchd agent (com.zcode.bot-healer, every 60 s):
it reads ONE mtime and, if the poller's transport heartbeat went stale,
kickstarts the poller's LaunchAgent. Nothing else.

Why so dumb: the in-process healer (zcode_bot.healer) heals CHANNELS —
threads and watchers inside live processes. THIS one heals the PROCESS
itself, so it must keep working when the bot's package is broken,
mid-deploy, or import-crashed: zero imports from zcode_bot, zero
network, one lstat + one launchctl. It is not the thing that dies.

Stale = hb-transport older than STALE_S (the poller beats every ~3 s
cycle; 180 s covers a deploy + resume + long poll with margin). Every
action lands in healer.log (plain append — the canonical engine may be
the thing that's dead).

Installed by deploy.sh (idempotent); manual:
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zcode.bot-healer.plist
"""
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
HB = os.path.join(HOME, ".zcode", "hooks", "state", "hb-transport")
LOG = os.path.join(HOME, ".zcode", "hooks", "logs", "healer.log")
POLLER_LABEL = "com.zcode.telegram-cmds"
STALE_S = 180.0


def _say(msg):
    try:
        with open(LOG, "a") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass  # silent-by-design: even this healer's log is best-effort


def main():
    try:
        age = time.time() - os.path.getmtime(HB)
    except OSError:
        _say("no hb-transport yet — waiting (the poller beats on its "
             "first cycle)")
        return 0
    if age <= STALE_S:
        return 0
    uid = os.getuid()
    try:
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", "gui/%d/%s" % (uid,
                                                            POLLER_LABEL)],
            capture_output=True, text=True, timeout=30)
        _say("hb stale %.0fs — kickstarted %s (rc=%d %s)"
             % (age, POLLER_LABEL, r.returncode, (r.stderr or "").strip()))
    except Exception as exc:  # noqa: BLE001 — this agent never dies loud
        _say("hb stale %.0fs — kickstart FAILED: %s" % (age, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
