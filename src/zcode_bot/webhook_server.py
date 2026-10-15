"""webhook_server — transporte webhook del bot (F3).

Recibe updates por HTTPS (Telegram → nosotros) en vez de long-polling.
Comparte con el polling el MISMO pipeline: normalize_update →
UpdateDispatcher → process_update_sync (serial, dedup por update_id,
owner fail-closed). Polling y webhook son MUTUAMENTE EXCLUYENTES (un
solo lector: ADR-001 → ADR-011); la elección vive en config
"transport": "polling"|"webhook".

Seguridad: Telegram firma cada delivery con el header
X-Telegram-Bot-Api-Secret-Token (param secret_token de setWebhook). El
receiver la compara en tiempo constante y FAIL-CLOSED: sin secret
configurado NADIE entra (401) — el hueco del 14/10 (bind 0.0.0.0 sin
auth, TODO de la línea 80) queda cerrado. Bind default 127.0.0.1;
exponerlo requiere port-forwarding/túnel (docs/runbook.md).

El server corre DENTRO del proceso gateway (runtime_async en gateway.py,
mismo LaunchAgent) — el standalone de abajo es solo para prueba manual.
"""
import asyncio
import hmac
import json
import logging
import os
import ssl

from aiohttp import web

from zcode_bot import config

log = logging.getLogger("webhook")

CERT_PATH = os.path.join(config.BASE, "hooks", "cert.pem")
KEY_PATH = os.path.join(config.BASE, "hooks", "key.pem")
PORT = 8443


def make_app(cfg, dispatcher, normalize, secret_getter):
    """App aiohttp. Inyectados: dispatcher (UpdateDispatcher), normalize
    (update crudo → msg o None), secret_getter (el secret se lee en cada
    request: un /set_webhook posterior no requiere reiniciar)."""

    async def telegram(request):
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        expected = secret_getter() or ""
        if not expected or \
                not hmac.compare_digest(provided.encode(), expected.encode()):
            log.warning("delivery sin secret válido desde %s",
                        request.remote)
            return web.Response(status=401, text="unauthorized")
        try:
            update = await request.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400, text="bad json")
        if not isinstance(update, dict):
            return web.Response(status=400, text="bad update")
        # 200 apenas autenticado y encolado — Telegram reintenta ante
        # no-200 y el dedup del dispatcher hace idempotente el re-envío
        msg = normalize(cfg, update)
        if msg is None:
            return web.json_response({"ok": True, "motivo": "ignorado"})
        ok, motivo = await dispatcher.submit(cfg, msg)
        return web.json_response({"ok": True, "motivo": motivo})

    async def health(request):
        return web.json_response({"ok": True})

    async def ready(request):
        return web.json_response({"ok": dispatcher is not None})

    app = web.Application()
    app.router.add_post("/telegram", telegram)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    return app


async def serve_async(cfg, dispatcher, normalize, secret_getter,
                      host="127.0.0.1", port=PORT):
    """Levanta el receiver con TLS y devuelve el runner (cleanup en el
    caller). Exige cert.pem/key.pem en ~/.zcode/hooks (self-signed vale:
    setWebhook lo registra con el parámetro certificate si hace falta)."""
    if not (os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH)):
        raise RuntimeError(
            "sin TLS: generá cert.pem/key.pem en ~/.zcode/hooks "
            "(openssl req -newkey rsa:2048 -nodes -keyout key.pem "
            "-x509 -days 365 -out cert.pem)")
    app = make_app(cfg, dispatcher, normalize, secret_getter)
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
    await site.start()
    log.info("webhook escuchando en %s:%d", host, port)
    return runner


# ------------------------------------------------- setWebhook/deleteWebhook

async def set_webhook(cfg, url, secret_token=None, certificate=None):
    """Configura el webhook en Telegram (AsyncTelegramClient)."""
    from zcode_bot.http_client import get_telegram_client
    params = {"url": url}
    if secret_token:
        params["secret_token"] = secret_token
    params["allowed_updates"] = ["message", "edited_message",
                                 "callback_query"]
    if certificate:
        params["certificate"] = certificate
    client = get_telegram_client(cfg)
    try:
        result = await client.post("setWebhook", params)
        return bool(result.get("ok"))
    except Exception as exc:  # noqa: BLE001
        log.error("setWebhook falló: %s", exc)
        return False


async def delete_webhook(cfg):
    """Elimina el webhook (vuelve a long-polling)."""
    from zcode_bot.http_client import get_telegram_client
    client = get_telegram_client(cfg)
    try:
        result = await client.post("deleteWebhook", {})
        return bool(result.get("ok"))
    except Exception as exc:  # noqa: BLE001
        log.error("deleteWebhook falló: %s", exc)
        return False


async def get_webhook_info(cfg):
    """Info del webhook actual (respuesta cruda de Telegram)."""
    from zcode_bot.http_client import get_telegram_client
    client = get_telegram_client(cfg)
    try:
        return await client.post("getWebhookInfo", {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------- prueba manual única

def _main():
    """python3 webhook_server.py — prueba manual (producción: el gateway
    lo hostea en runtime_async; ver ADR-011)."""
    logging.basicConfig(level=logging.INFO)
    from zcode_bot.config import load_config
    from zcode_bot.dispatcher import UpdateDispatcher
    from zcode_bot.updates import normalize_update, from_owner
    from zcode_bot.gateway import process_update_sync

    cfg = load_config()
    dispatcher = UpdateDispatcher(
        process_update_sync, from_owner,
        on_error=lambda e: log.error("dispatcher: %s", e))
    consumer = asyncio.ensure_future(dispatcher.run())

    async def _run():
        runner = await serve_async(
            cfg, dispatcher, normalize_update,
            lambda: cfg.get("webhook_secret"))
        try:
            await asyncio.Event().wait()  # hasta Ctrl-C
        finally:
            await runner.cleanup()
            consumer.cancel()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _main()
