"""Logging del bot — cuerpos canónicos (consolidación F1e: el gateway
definía estas mismas funciones y pisaba este módulo; UN cuerpo vivo).
Paths por atributo (config.LOG_DIR / config.BASE): fixture-aware en tests.
"""
import datetime
import json
import os
import time

from zcode_bot import config


def log(kind, msg):
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(config.LOG_DIR, f"{kind}.log"), "a") as f:
            f.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def log_payload(event, payload):
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(config.LOG_DIR, f"payload-{stamp}-{event}.json"),
                  "w") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    except (OSError, TypeError):
        pass


def pwlog(*a):
    """Log de pasos del bot unificado (worker + entradas del poller).
    F24: con FECHA (la reconstrucción forense del 13/10 exigió cruzar 3
    fuentes para fechar líneas) y rotación por tamaño."""
    try:
        p = os.path.join(config.BASE, "hooks", "logs", "prompt-worker.log")
        _log_rotate(p)
        with open(p, "a") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now()
                                   .strftime("%m-%d %H:%M:%S"),
                                   " ".join(str(x) for x in a)))
    except OSError:
        pass


def _log_rotate(path, max_bytes=5 * 1024 * 1024):
    """>5MB → .1 (una generación: el log es diagnóstico, no archivo)."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass
