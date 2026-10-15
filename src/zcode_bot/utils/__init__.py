"""Utilidades comunes para zcode_bot.

Fuentes únicas por concepto (consolidación 15/10, F1):
- config: paths y configuración (BASE/STATE_DIR/...)
- formatting: esc/preview/fmt_*/datetime_dt/task_label/labeled
- validation: norm/classify/first
- http: tg_api/send/send_document (síncronos; el async vive en http_client)
- logging: log/pwlog/log_payload
"""
from .config import load_config, save_config, DEFAULTS, configured, HOME, BASE, CONFIG_PATH, STATE_DIR, LOG_DIR, DEFAULT_WS, API
from .formatting import esc, preview, fmt_tok, fmt_num, datetime_dt, task_label, labeled
from .validation import classify, first, norm
from .http import tg_api, send, send_document
from .logging import log, pwlog, log_payload

__all__ = [
    "load_config", "save_config", "DEFAULTS", "configured",
    "HOME", "BASE", "CONFIG_PATH", "STATE_DIR", "LOG_DIR", "DEFAULT_WS", "API",
    "esc", "preview", "fmt_tok", "fmt_num", "datetime_dt",
    "task_label", "labeled",
    "classify", "first", "norm",
    "tg_api", "send", "send_document",
    "log", "pwlog", "log_payload",
]
