"""utils.config — compatibility shim. The single source is
zcode_bot.config (F1e): same content, one living body."""
from zcode_bot.config import (  # noqa: F401
    API, BASE, CONFIG_PATH, DEFAULTS, DEFAULT_WS, HOME, LOG_DIR, STATE_DIR,
    configured, load_config, save_config,
)
