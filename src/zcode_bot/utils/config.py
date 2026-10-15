"""utils.config — shim de compatibilidad. La fuente única es
zcode_bot.config (F1e): mismo contenido, un solo cuerpo vivo."""
from zcode_bot.config import (  # noqa: F401
    API, BASE, CONFIG_PATH, DEFAULTS, DEFAULT_WS, HOME, LOG_DIR, STATE_DIR,
    configured, load_config, save_config,
)
