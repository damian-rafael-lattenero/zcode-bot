"""http_client — compatibility shim. The single source is
zcode_bot.telegram.client (F2a: async transport package)."""
from zcode_bot.telegram.client import *  # noqa: F401,F403
from zcode_bot.telegram.client import (  # noqa: F401
    DEFAULT_ENDPOINTS, AsyncTelegramClient, TelegramEndpointConfig,
    get_telegram_client,
)
