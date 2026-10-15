"""http_client — shim de compatibilidad. La fuente única es
zcode_bot.telegram.client (F2a: paquete del transporte async)."""
from zcode_bot.telegram.client import *  # noqa: F401,F403
from zcode_bot.telegram.client import (  # noqa: F401
    DEFAULT_ENDPOINTS, AsyncTelegramClient, TelegramEndpointConfig,
    get_telegram_client,
)
