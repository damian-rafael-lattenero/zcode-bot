#!/usr/bin/env python3
"""Shim/Facade del gateway Telegram↔ZCode.

El LaunchAgent y los hooks de ZCode siguen llamando a ESTE path con los
subcomandos de siempre (hook | watch-commands | watch-app | register |
set-token | test | quota | --stuck-watch). La implementación vive en el
paquete zcode_bot (gateway.py) — este archivo solo delega.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zcode_bot.gateway import main  # noqa: E402

if __name__ == "__main__":
    main()
