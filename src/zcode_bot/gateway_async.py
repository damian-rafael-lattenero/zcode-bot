"""Gateway Async — versión async del gateway de Telegram para ZCode.
 
Reemplaza las funciones síncronas con sleep por versiones reactivas usando
asyncio, fswatch hooks, y el AsyncTelegramClient con circuit breaker.
"""
import asyncio
import json
import os
import sys
import traceback
from typing import Any, Optional, Tuple, Union

# Importar módulos locales
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zcode_bot import state as zc_state
from zcode_bot import concurrency as zc_conc
from zcode_bot import proto as zc_proto
from zcode_bot import cli_logs as zc_logs
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import fswatch
from zcode_bot import commands as zc_commands
from zcode_bot import reports as zc_reports

from zcode_bot.http_client import AsyncTelegramClient
from zcode_bot.rate_limiter import AsyncRateLimiter
from zcode_bot.circuit_breaker import CircuitBreaker

# Constantes (copiadas del gateway original)
HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".zcode")
STATE_DIR = os.path.join(BASE, "hooks", "state")
LOG_DIR = os.path.join(BASE, "hooks", "logs")
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
}

# Cliente Telegram async global (inicializado bajo demanda)
_telegram_client: Optional[AsyncTelegramClient] = None


async def get_telegram_client() -> AsyncTelegramClient:
    """Obtiene o crea el cliente Telegram async singleton."""
    global _telegram_client
    if _telegram_client is None:
        cfg = load_config()
        token = cfg.get("token")
        if not token:
            raise RuntimeError("Token de Telegram no configurado")
        _telegram_client = AsyncTelegramClient(token)
    return _telegram_client


def load_config():
    cfg = dict(DEFAULTS)
    try:
        config_path = os.environ.get("ZT_CONFIG", os.path.join(BASE, "telegram-remote.json"))
        with open(config_path) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    config_path = os.environ.get("ZT_CONFIG", os.path.join(BASE, "telegram-remote.json"))
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(config_path, 0o600)


def log(kind, msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(LOG_DIR, f"{kind}.log"), "a") as f:
            f.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


import time  # para log timestamp


def configured(cfg):
    return bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))


async def send_async(cfg, text, loud=None, markup=None):
    """Envía un mensaje usando AsyncTelegramClient (reactivo, sin sleeps)."""
    if not configured(cfg):
        return False
    if loud is None:
        loud = bool(cfg.get("loud_all", False))
    
    client = await get_telegram_client()
    try:
        await client.send_message(
            chat_id=cfg["chat_id"],
            text=text[:4096],
            parse_mode="HTML",
            disable_notification=not loud,
            reply_markup=json.dumps(markup) if markup else None,
        )
        return True
    except Exception as exc:
        log("errors", f"send_async falló: {exc}")
        return False


def send(cfg, text, loud=None, markup=None):
    """Wrapper síncrono para compatibilidad - delega a send_async en executor."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(send_async(cfg, text, loud, markup))


def _store(name):
    return zc_state.AtomicStore(os.path.join(STATE_DIR, name))


def _outbox_put(text):
    def add(box):
        box = box if isinstance(box, list) else []
        box.append({"ts": int(time.time()), "text": text[:4096]})
        return box[-20:]
    try:
        _store("outbox.json").update_cross_process(add, [])
    except OSError:
        pass


async def outbox_flush_async(cfg):
    """Reenvía mensajes de outbox de forma reactiva."""
    if not configured(cfg):
        return 0
    client = await get_telegram_client()
    ok_count = [0]
    
    def drain(box):
        box = box if isinstance(box, list) else []
        remaining = []
        for item in box:
            try:
                loop = asyncio.get_event_loop()
                loop.run_until_complete(client.send_message(
                    chat_id=cfg["chat_id"],
                    text=item["text"],
                    parse_mode="HTML",
                    disable_notification=True,
                ))
                ok_count[0] += 1
            except Exception:
                remaining.append(item)
        return remaining
    
    try:
        _store("outbox.json").update_cross_process(drain, [])
    except OSError:
        pass
    return ok_count[0]


def outbox_flush(cfg):
    """Wrapper síncrono."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(outbox_flush_async(cfg))


def current_offset():
    try:
        with open(os.path.join(STATE_DIR, "tg-offset")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def save_offset(value):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "tg-offset"), "w") as f:
        f.write(str(value))


def busy_path():
    return os.path.join(STATE_DIR, "tg-busy")


def busy_fresh(max_age=None):
    if max_age is None:
        max_age = int(load_config().get("wait_timeout_s", 240)) + 60
    try:
        return time.time() - os.path.getmtime(busy_path()) < max_age
    except OSError:
        return False


# El resto de helpers (fetch_updates, drain_updates, from_owner, etc.)
# se mantienen síncronos por ahora y se llaman desde asyncio.to_thread

async def fetch_updates_async(cfg, offset, timeout):
    """Versión async de fetch_updates."""
    return await asyncio.to_thread(fetch_updates, cfg, offset, timeout)


def fetch_updates(cfg, offset, timeout):
    """Síncrona para compatibilidad."""
    client = asyncio.run(get_telegram_client()) if not hasattr(fetch_updates, '_client') else None
    # Esta se mantiene síncrona por ahora, se usa desde asyncio.to_thread
    import urllib.request
    token = cfg.get("token") or ""
    url = f"{API}/bot{token}/getUpdates"
    params = {"offset": offset, "timeout": timeout, "limit": 100}
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
        res = json.loads(resp.read().decode())
    updates = res.get("result", [])
    messages = []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        cb = u.get("callback_query")
        if cb:
            # callback_query -> pseudo mensaje
            pseudo = dict(cb.get("message") or {})
            if cb.get("from"):
                pseudo["from"] = cb["from"]
            pseudo["text"] = _cb_text(cb.get("data") or "")
            pseudo["_cb_data"] = cb.get("data") or ""
            messages.append(pseudo)
            continue
        msg = u.get("message") or u.get("edited_message")
        if msg and msg.get("text"):
            messages.append(msg)
    return offset, messages, len(updates)


def _cb_text(data):
    if data.startswith("t:ses"):
        return "/task @" + data[2:]
    if data.startswith("s:ses"):
        return "/stop @" + data[2:]
    if data.startswith("c:ses"):
        return "/compact @" + data[2:]
    if data.startswith("mo:"):
        return "/mode " + data[3:]
    if data.startswith("qgo:"):
        return "/qgo " + data[4:]
    if data.startswith("qdel:"):
        return "/qdel " + data[5:]
    if data == "retry":
        return "/retry"
    if data.startswith("m:"):
        return "/model " + data[2:]
    if data.startswith("th:"):
        return "/thinking " + data[3:]
    if data.startswith("task:"):
        return "/all_tasks " + data[5:]
    if data.startswith("ans:"):
        return data[4:]
    if data == "yes":
        return "si"
    if data == "no":
        return "no"
    if data == "nuc:go":
        return "/nuclear go"
    if data == "nuc:no":
        return "/nuclear no"
    if data == "dep:go":
        return "/deploy go"
    if data == "dep:no":
        return "/deploy no"
    return data[:64]


async def drain_updates_async(cfg):
    """Versión async de drain_updates."""
    offset = current_offset()
    messages = []
    for _ in range(10):
        try:
            offset, batch, n = await fetch_updates_async(cfg, offset, 0)
        except Exception as exc:
            log("errors", f"getUpdates falló: {exc}")
            return offset, messages
        messages.extend(batch)
        if n < 100:
            return offset, messages
    return offset, messages


def from_owner(cfg, msg):
    owner = cfg.get("owner_user_id")
    return owner is None or msg.get("from", {}).get("id") == owner


# --- wait_reply async reactiva ---

async def wait_reply_async(cfg, hint):
    """Espera reactiva por respuesta de Telegram usando fswatch hooks."""
    deadline = time.time() + int(cfg.get("wait_timeout_s", 240))
    offset, stale = await drain_updates_async(cfg)
    save_offset(offset)
    os.makedirs(STATE_DIR, exist_ok=True)
    
    try:
        while time.time() < deadline:
            with open(busy_path(), "w") as f:
                f.write(str(time.time()))
            remaining = int(deadline - time.time())
            poll_timeout = max(1, min(25, remaining))
            
            try:
                offset, batch, _ = await fetch_updates_async(cfg, offset, poll_timeout)
                save_offset(offset)
            except Exception as exc:
                log("errors", f"getUpdates (wait) falló: {exc}")
                await asyncio.sleep(3)
                continue
            
            for msg in batch:
                if not from_owner(cfg, msg):
                    continue
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    reply = handle_command(cfg, text)
                    if reply is not None:
                        if isinstance(reply, tuple):
                            await send_async(cfg, reply[0], loud=False, markup=reply[1])
                        else:
                            await send_async(cfg, reply, loud=False)
                        continue
                kind = classify(text)
                return (kind if kind in ("yes", "no") else "text"), text
            
            if not batch:
                # El long-poll ya bloquea hasta timeout, no necesitamos sleep
                pass
        return None, None
    finally:
        try:
            os.remove(busy_path())
        except OSError:
            pass


def wait_reply(cfg, hint):
    """Wrapper síncrono."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(wait_reply_async(cfg, hint))


# --- classify, esc, etc. (puros, sin cambios) ---

def norm(s):
    import unicodedata
    import re
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


YES_EXACT = {"si", "yes", "y", "ok", "okay", "dale", "1", "acepto", "go", "apru", "apruébalo"}
NO_EXACT = {"no", "n", "nop", "nope", "2", "never", "núnca"}
YES_PREFIX = ("aprob", "allow", "permit", "acept", "autoriz")
NO_PREFIX = ("deny", "deneg", "rechaz", "bloque", "cancel", "nope")


def classify(text):
    t = norm(text)
    if not t:
        return "text"
    first = re.sub(r"[!?.,;:¡¿]+$", "", t.split(" ", 1)[0])
    if first in YES_EXACT or t in YES_EXACT:
        return "yes"
    if first in NO_EXACT or t in NO_EXACT:
        return "no"
    if any(first.startswith(p) for p in YES_PREFIX):
        return "yes"
    if any(first.startswith(p) for p in NO_PREFIX):
        return "no"
    return "text"


import re


def esc(s):
    return (s or "").replace("&", "&").replace("<", "<").replace(">", ">")


def preview(value, limit=280):
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def first(payload, *keys):
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return None


# Exportar funciones síncronas originales para compatibilidad
# (se pueden ir migrando progresivamente)
__all__ = [
    "load_config", "save_config", "log", "configured", "send", "send_async",
    "_store", "_outbox_put", "outbox_flush", "outbox_flush_async",
    "current_offset", "save_offset", "busy_path", "busy_fresh",
    "fetch_updates", "fetch_updates_async", "drain_updates", "drain_updates_async",
    "from_owner", "wait_reply", "wait_reply_async",
    "norm", "classify", "esc", "preview", "first",
]