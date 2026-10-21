#!/usr/bin/env python3
"""Compatibility shim/facade for the Telegram↔ZCode gateway.

The LaunchAgent and ZCode hooks keep calling THIS path with the usual
subcommands (hook | watch-commands | watch-app | register |
set-token | test | quota | --stuck-watch | --espectador | --run-turn |
--nuclear-watchdog). The implementation
lives in the zcode_bot package (gateway.py) — this file only delegates.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zcode_bot.gateway import main  # noqa: E402

if __name__ == "__main__":
    main()
