"""config — LA fuente única de paths y configuración del bot (F1e).

Todos los módulos leen los paths COMO ATRIBUTO (config.STATE_DIR) en
call-time: así el fixture de tests rebindea UNA vez acá y desaparece la
divergencia gateway-vs-utils (el bug histórico: el gateway leía el
fixture y utils leía producción). Nada debe hacer `from config import
STATE_DIR` (copia el valor en el import — esa copia es la divergencia).
"""
import json
import os

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".zcode")
CONFIG_PATH = os.environ.get("ZT_CONFIG", os.path.join(BASE, "telegram-remote.json"))
STATE_DIR = os.path.join(BASE, "hooks", "state")
LOG_DIR = os.path.join(BASE, "hooks", "logs")
DEFAULT_WS = os.path.join(BASE, "workspace", "default")
API = os.environ.get("ZT_API_BASE", "https://api.telegram.org")

DEFAULTS = {
    "token": "",
    "chat_id": None,
    "owner_user_id": None,
    "owner_name": "",
    "wait_timeout_s": 240,
    "question_wait_timeout_s": 60,
    "question_mode": "mac",
    "stuck_after_min": 20,
    "loud_all": True,
    "suppress_error_patterns": [
        "File has not been read yet",
        "File does not exist",
        "File has been (?:unexpectedly )?modified",
        "String to replace not found",
        "Found \\d+ matches? of the string to replace",
    ],
    "notify_prompt": True,
    "notify_stop": True,
    "notify_failures": True,
    "notify_session": True,
    "only_active_task": True,
    "enabled": True,
    # F8 (ADR-014): worker caliente — el runtime espera el próximo prompt
    # de la sesión durante estos segundos (0 = un proceso por turno, como
    # siempre). Y rotación: transcripto (MB) que dispara compact+resumen+
    # sesión nueva sembrada (0 = off). Ver workers._warm_wait/_rotar_sesion.
    "warm_keepalive_s": 600,
    "rotar_umbral_mb": 8,
    # ADR-011: transporte de updates, mutuamente excluyentes
    "transport": "polling",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def configured(cfg):
    return bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))
