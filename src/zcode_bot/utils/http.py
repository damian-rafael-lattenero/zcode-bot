"""Utilidades HTTP para Telegram Bot API."""
import json
import logging
import time
import urllib.request

from .config import API, load_config

logger = logging.getLogger(__name__)


def tg_api(cfg, method, params=None, timeout=35):
    """Llamada a Telegram Bot API."""
    token = cfg.get("token") or ""
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send(cfg, text, loud=None, markup=None):
    """Envía un mensaje con reintentos; devuelve True si salió. Nunca lanza."""
    if not configured(load_config()):
        return False
    cfg_full = load_config()
    if loud is None:
        loud = bool(cfg_full.get("loud_all", False))
    params = {
        "chat_id": cfg_full["chat_id"],
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_notification": not loud,
    }
    if markup:
        params["reply_markup"] = json.dumps(markup)
    delays = (0, 1, 3)
    for attempt, delay in enumerate(delays, 1):
        if delay:
            time.sleep(delay)
        try:
            tg_api(cfg_full, "sendMessage", params)
            return True
        except Exception as exc:
            log("errors", f"sendMessage falló (intento {attempt}/{len(delays)}): {exc}")
    _outbox_put(text)
    return False


def _live_send(cfg, text):
    """sendMessage que devuelve message_id (para el placeholder en vivo)."""
    try:
        r = tg_api(load_config(), "sendMessage",
                   {"chat_id": load_config()["chat_id"], "text": text[:3900],
                    "parse_mode": "HTML", "disable_notification": True})
        return (r.get("result") or {}).get("message_id")
    except Exception:
        return None


def _live_edit(cfg, msg_id, text):
    """editMessageText best-effort."""
    try:
        tg_api(load_config(), "editMessageText",
               {"chat_id": load_config()["chat_id"], "message_id": msg_id,
                "text": text[:3900], "parse_mode": "HTML"})
        return True
    except Exception as exc:
        if "429" in str(exc) or "Too Many" in str(exc):
            time.sleep(5)
        return False


def send_document(cfg, filename, text, caption=""):
    """Manda un .txt como documento de Telegram."""
    if not configured(load_config()):
        return False
    try:
        import uuid
        b = "----zcb" + uuid.uuid4().hex
        body = b""
        fields = {"chat_id": str(cfg["chat_id"]), "caption": caption[:900]}
        for k, v in fields.items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
                     "%s\r\n" % (b, k, v)).encode()
        body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; "
                 "filename=\"%s\"\r\nContent-Type: text/plain\r\n\r\n"
                 % (b, filename)).encode()
        body += text.encode("utf-8") + ("\r\n--%s--\r\n" % b).encode()
        req = urllib.request.Request(
            "%s/bot%s/sendDocument" % (API, cfg.get("token") or ""),
            data=body, headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
        with urllib.request.urlopen(req, timeout=90) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as exc:
        log("errors", "sendDocument: %s" % exc)
        return False


def _outbox_put(text):
    """Encola un mensaje para cuando la red vuelva."""
    from zcode_bot.state import AtomicStore
    STATE_DIR = os.path.join(os.path.expanduser("~"), ".zcode", "hooks", "state")
    STORE = AtomicStore(os.path.join(STATE_DIR, "outbox.json"))
    def add(box):
        box = box if isinstance(box, list) else []
        box.append({"ts": int(time.time()), "text": text[:3900]})
        return box[-20:]
    try:
        STORE.update_cross_process(add, [])
    except OSError:
        pass


import os
import time
from zcode_bot.state import AtomicStore
from zcode_bot.utils.config import load_config, configured
from zcode_bot.utils.logging import log