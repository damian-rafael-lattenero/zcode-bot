"""Webhook Server — recibe updates de Telegram via HTTPS (elimina long-polling).
 
Patrón: aiohttp server + AsyncTelegramClient para enviar respuestas.
Elimina toda la clase de bugs de long-polling (offsets, 409, race conditions,
latencia 3s). Latencia típica ~50ms.
 
Setup:
  1. Tener dominio con TLS (ej: bot.example.com)
  2. zcode-telegram.py set-webhook https://bot.example.com/telegram
  3. El bot responde instantáneo a cada update.
"""
import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

from aiohttp import web

# Config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zcode_bot import http_client as zc_http
from zcode_bot.utils.config import load_config, save_config
from zcode_bot.utils.validation import classify
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.commands import CommandRegistry
from zcode_bot.commands.registry import REG

LOG_DIR = os.path.join(os.path.expanduser("~"), ".zcode", "hooks", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "webhook.log")),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


class WebhookServer:
    """Servidor aiohttp que recibe updates de Telegram via webhook."""

    def __init__(self, config: dict):
        self.config = config
        self.client: Optional[zc_http.AsyncTelegramClient] = None
        self.app = web.Application()
        self.app.router.add_post("/telegram", self.handle_update)
        self.app.router.add_get("/health", self.health_check)
        self.app.router.add_get("/ready", self.ready_check)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def initialize(self):
        """Inicializa el cliente Telegram."""
        self.client = await zc_http.get_telegram_client(self.config)
        log.info("WebhookServer initialized")

    async def health_check(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "healthy"})

    async def ready_check(self, request: web.Request) -> web.Response:
        # Verifica que el cliente Telegram esté listo
        if self.client and not self.client._session.closed:
            return web.json_response({"status": "ready"})
        return web.json_response({"status": "not ready"}, status=503)

    async def handle_update(self, request: web.Request) -> web.Response:
        """Procesa un update de Telegram (webhook)."""
        try:
            data = await request.json()
        except Exception as e:
            log.error(f"Invalid JSON in webhook: {e}")
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        # Valida que venga de Telegram (opcional: verificar secret token)
        # TODO: añadir validación de X-Telegram-Bot-Api-Secret-Token header

        # Procesa el update de forma asíncrona (no bloquea el webhook)
        asyncio.create_task(self.process_update(data))

        return web.json_response({"ok": True})

    async def process_update(self, data: dict):
        """Procesa un update de Telegram usando la lógica existente del gateway."""
        try:
            # Reutiliza la lógica de procesamiento del gateway
            cfg = load_config()
            
            # Extrae el mensaje del update
            if "message" in data:
                msg = data["message"]
            elif "edited_message" in data:
                msg = data["edited_message"]
            elif "callback_query" in data:
                # Convierte callback_query a pseudo-mensaje
                cb = data["callback_query"]
                msg = self._callback_to_message(cb)
            else:
                return  # Update type no manejado

            # Verifica owner
            if not classify(msg):
                return
            
            # Verifica chat_id
            if msg.get("chat", {}).get("id") != cfg.get("chat_id"):
                log.warning(f"UPDATE de otro chat {msg.get('chat', {}).get('id')} — ignorado")
                return

            text = (msg.get("text") or "").strip()
            if not text:
                return

            # Procesa comando o texto libre
            try:
                if not text.startswith("/"):
                    reply = REG.handle_free_text(cfg, text)
                else:
                    reply = REG.handle_command(cfg, text)
                    if reply is None:
                        reply = "🤔 Comando desconocido — /help"
            except Exception as exc:
                log.error(f"handler error: {exc}")
                reply = f"⚠️ Error interno: {exc}"

            # Envía respuesta
            if reply is not None:
                if isinstance(reply, tuple):
                    await self.send_async(cfg, reply[0], loud=False, markup=reply[1])
                else:
                    await self.send_async(cfg, reply, loud=False)

        except Exception as e:
            log.error(f"Error processing update: {e}")

    def _callback_to_message(self, cb: dict) -> dict:
        """Convierte callback_query a pseudo-mensaje."""
        pseudo = dict(cb.get("message") or {})
        if cb.get("from"):
            pseudo["from"] = cb["from"]
        pseudo["text"] = esc(preview((cb.get("data") or "")[:200]))
        pseudo["_cb_data"] = cb.get("data") or ""
        return pseudo

    async def send_async(self, cfg: dict, text: str, loud: bool = False, markup: dict = None):
        """Envía mensaje via AsyncTelegramClient."""
        if not self.client:
            return
        try:
            await self.client.send_message(
                chat_id=cfg["chat_id"],
                text=text[:4096],
                parse_mode="HTML",
                disable_notification=not loud,
                reply_markup=json.dumps(markup) if markup else None,
            )
        except Exception as e:
            log.error(f"send_async failed: {e}")

    async def start(self, host: str = "0.0.0.0", port: int = 8443, ssl_context=None):
        """Inicia el servidor webhook."""
        if not self.client:
            await self.initialize()
        
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port, ssl_context=ssl_context)
        await self._site.start()
        log.info(f"Webhook server listening on {host}:{port}")

    async def stop(self):
        """Detiene el servidor."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        if self.client:
            await self.client.close()
        log.info("Webhook server stopped")


# Funciones de configuración de webhook

async def set_webhook(config: dict, url: str, secret_token: Optional[str] = None) -> bool:
    """Configura el webhook en Telegram."""
    client = zc_http.get_telegram_client(config)
    params = {"url": url}
    if secret_token:
        params["secret_token"] = secret_token
    params["allowed_updates"] = ["message", "edited_message", "callback_query"]
    
    try:
        result = await client.post("setWebhook", params)
        return result.get("ok", False)
    except Exception as e:
        log.error(f"setWebhook failed: {e}")
        return False


async def delete_webhook(config: dict) -> bool:
    """Elimina el webhook (vuelve a long-polling)."""
    client = zc_http.get_telegram_client(config)
    try:
        result = await client.post("deleteWebhook", {})
        return result.get("ok", False)
    except Exception as e:
        log.error(f"deleteWebhook failed: {e}")
        return False


async def get_webhook_info(config: dict) -> dict:
    """Obtiene info del webhook actual."""
    client = zc_http.get_telegram_client(config)
    try:
        return await client.post("getWebhookInfo", {})
    except Exception as e:
        log.error(f"getWebhookInfo failed: {e}")
        return {"ok": False, "error": str(e)}


# Comandos CLI para webhook

def cmd_set_webhook(url: str, secret_token: Optional[str] = None):
    """Configura el webhook: zcode-telegram.py set-webhook <URL> [secret]"""
    config = load_config()
    if not config.get("token"):
        print("Falta token: zcode-telegram.py set-token <TOKEN>")
        return False
    
    ok = asyncio.run(set_webhook(config, url, secret_token))
    if ok:
        print(f"✅ Webhook configurado: {url}")
        # Guarda URL en config para referencia
        config["webhook_url"] = url
        if secret_token:
            config["webhook_secret"] = secret_token
        save_config(config)
        return True
    else:
        print("❌ Falló configuración de webhook")
        return False


def cmd_delete_webhook():
    """Elimina webhook: zcode-telegram.py delete-webhook"""
    config = load_config()
    ok = asyncio.run(delete_webhook(config))
    if ok:
        print("✅ Webhook eliminado (vuelve a long-polling)")
        config.pop("webhook_url", None)
        config.pop("webhook_secret", None)
        save_config(config)
        return True
    else:
        print("❌ Falló eliminación de webhook")
        return False


def cmd_webhook_info():
    """Info del webhook: zcode-telegram.py webhook-info"""
    config = load_config()
    info = asyncio.run(get_webhook_info(config))
    print(json.dumps(info, indent=2, ensure_ascii=False))


# Main entrypoint para webhook server

async def run_webhook_server():
    """Ejecuta el servidor webhook (para systemd/launchd)."""
    config = load_config()
    
    if not config.get("webhook_url"):
        print("Falta webhook_url en config. Ejecuta: zcode-telegram.py set-webhook <URL>")
        return 1
    
    server = WebhookServer(config)
    
    # Configura SSL (necesario para Telegram)
    import ssl
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    cert_path = os.path.expanduser("~/.zcode/hooks/cert.pem")
    key_path = os.path.expanduser("~/.zcode/hooks/key.pem")
    
    if os.path.exists(cert_path) and os.path.exists(key_path):
        ssl_context.load_cert_chain(cert_path, key_path)
    else:
        print("⚠️ Sin cert/key TLS. Genera cert.pem y key.pem en ~/.zcode/hooks/")
        return 1
    
    await server.start(host="0.0.0.0", port=8443, ssl_context=ssl_context)
    
    # Mantiene corriendo
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run-webhook":
        asyncio.run(run_webhook_server())