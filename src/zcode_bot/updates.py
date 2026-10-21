"""updates — Telegram updates plumbing (F4h).

Atomic offset, busy-flag for waits, callback normalization into
pseudo-messages, long-poll, fail-closed owner filter and the poller's
housekeeping. One pipeline: polling and webhook share normalize→
dispatcher (ADR-011). Transport BY ATTRIBUTE (uhttp.tg_api).
"""
import glob
import json
import os
import time

from zcode_bot import config
from zcode_bot.core import _questions
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.validation import first
from zcode_bot.utils.logging import log, pwlog, _log_rotate
from zcode_bot.utils.config import load_config


def busy_path():
    return os.path.join(config.STATE_DIR, "tg-busy")


# F52: busy-flag freshness, INDEPENDENT of wait_timeout_s (24 h of
# waiting must not mean 24 h of a mute bot if the hook dies holding the
# flag: wait_reply rewrites the flag on every round; once the window
# expires, the poller wakes up on its own).
BUSY_FRESCO_S = 360


# A .log.1 has already been rotated OUT of the way (diagnostics for a
# closed incident); if it also weighs more than this, it is not a log,
# it is a flood — the 294MB errors.log.1 of the 19/10 incident (3.1M
# identical tracebacks in ~5min) had no reason to outlive its own
# housekeeping.
LOG_ROTADO_CAP_BYTES = 50 * 1024 * 1024


def _logs_rotados_gordos():
    """Purges *.log.1 files over the cap: write-time rotation (log())
    bounds the live flood, but the .1 it leaves behind cannot be left
    unbounded either."""
    n = 0
    for ruta in glob.glob(os.path.join(config.LOG_DIR, "*.log.1")):
        try:
            if os.path.getsize(ruta) > LOG_ROTADO_CAP_BYTES:
                os.remove(ruta)
                n += 1
        except OSError:
            continue
    return n


def busy_fresh(max_age=None):
    """True while a permission/question wait is active (the poller must
    not touch getUpdates). F52: the window NO LONGER derives from
    wait_timeout_s — with 24 h waits that meant 24 h of a mute bot if
    the hook died holding the flag. Its own window: wait_reply rewrites
    the flag on EVERY round (long-poll ≤25 s), so 6 min of margin is
    plenty; once expired, the poller goes back to getUpdates even if
    the wait is still configured long."""
    if max_age is None:
        max_age = BUSY_FRESCO_S
    try:
        return time.time() - os.path.getmtime(busy_path()) < max_age
    except OSError:
        return False


def current_offset():
    try:
        with open(os.path.join(config.STATE_DIR, "tg-offset")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def save_offset(value):
    # atomic (tmp + fsync + rename): a crash mid-write used to leave the
    # offset truncated → ValueError → offset 0 → massive re-processing
    # (same pattern as AtomicStore). The tmp carries the pid: the poller
    # is not the only writer (cmd_register and the drain also save) and
    # with a fixed name the loser did os.replace on a tmp the other one
    # had already moved (20/10 18:10: ENOENT 'tg-offset.tmp' -> 'tg-offset')
    os.makedirs(config.STATE_DIR, exist_ok=True)
    path = os.path.join(config.STATE_DIR, "tg-offset")
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(str(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


from zcode_bot.commands.callbacks import texto_de_callback  # noqa: E402

# R5: the prefix→command table lives in commands/callbacks.py (a single
# body); the alias keeps the historical name that the gateway and the
# test suite import from here.
_cb_text = texto_de_callback


def answer_callback(cfg, cb):
    """Best-effort answerCallbackQuery: stops the tapped button's spinner."""
    try:
        uhttp.tg_api(cfg, "answerCallbackQuery", {"callback_query_id": cb.get("id") or ""})
    except Exception as exc:  # noqa: BLE001
        log("errors", f"answerCallbackQuery: {exc}")


def react(cfg, msg, emoji):
    """F23 — setMessageReaction (Bot API 7.0+): feedback on the ORIGINAL
    message without sending a single new one. 👀 on receipt, ✅ on
    delivery. Fully best-effort: if the server does not support it,
    silence."""
    mid = (msg or {}).get("message_id")
    if not mid:
        return
    try:
        uhttp.tg_api(cfg, "setMessageReaction",
               {"chat_id": cfg["chat_id"], "message_id": mid,
                "reaction": [{"type": "emoji", "emoji": emoji}]})
    except Exception:  # noqa: BLE001
        pass


def typing(cfg, seconds=5):
    """F23 — sendChatAction 'typing': kills the mute window between the
    owner's message and the worker's placeholder. Best-effort."""
    try:
        uhttp.tg_api(cfg, "sendChatAction",
               {"chat_id": cfg["chat_id"], "action": "typing"})
    except Exception:  # noqa: BLE001
        pass


def normalize_update(cfg, u):
    """Raw Telegram update → internal-flow message (or None if the
    update does not interest us). The SAME translation for polling and
    webhook (F3): inline buttons → pseudo-message with equivalent text
    (_cb_text) — everything downstream (commands, wait_reply, questions)
    works unchanged. Stamps update_id (dispatcher dedup in webhook)."""
    cb = u.get("callback_query")
    if cb:
        answer_callback(cfg, cb)
        data = cb.get("data") or ""
        pseudo = dict(cb.get("message") or {})
        if cb.get("from"):
            pseudo["from"] = cb["from"]
        pseudo["text"] = _cb_text(data)
        pseudo["_cb_data"] = data
        pseudo["update_id"] = u.get("update_id")
        return pseudo
    msg = u.get("message") or u.get("edited_message")
    if msg and msg.get("text"):
        msg = dict(msg)
        msg["update_id"] = u.get("update_id")
        return msg
    return None


def fetch_updates(cfg, offset, timeout):
    res = uhttp.tg_api(cfg, "getUpdates", {"offset": offset, "timeout": timeout, "limit": 100},
                 timeout=timeout + 15)
    updates = res.get("result", [])
    messages = []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        m = normalize_update(cfg, u)
        if m is not None:
            messages.append(m)
    return offset, messages, len(updates)


def drain_updates(cfg):
    """Drains pending messages and returns (final_offset, messages)."""
    offset = current_offset()
    messages = []
    for _ in range(10):
        try:
            offset, batch, n = fetch_updates(cfg, offset, 0)
        except Exception as exc:  # noqa: BLE001
            log("errors", f"getUpdates falló: {exc}")
            return offset, messages
        messages.extend(batch)
        if n < 100:
            return offset, messages
    return offset, messages


def from_owner(cfg, msg):
    """Fail-closed: with no owner_user_id in config, NOBODY gets through.
    Registration happens via CLI (zcode-telegram.py register), never by
    accepting the first message that arrives — before, a config without
    an owner let anyone who wrote to the bot operate it."""
    owner = cfg.get("owner_user_id")
    if owner is None:
        log("errors", "from_owner: config sin owner_user_id — update "
                      "descartado (fail-closed; registrar con "
                      "`zcode-telegram.py register`)")
        return False
    return msg.get("from", {}).get("id") == owner


def _hq_pending():
    return _questions().any_pending()


def _reload_now_path():
    return os.path.join(config.STATE_DIR, config.ARCH_RELOAD)


def housekeeping():
    """F24 — purges what piles up with no ceiling (13/10 audit:
    23 backups never purged, 964 payload-*.json with plaintext prompts,
    178 empty proto-stderr files, orphan q-*.json, old markers). Runs
    ONCE at poller startup. All best-effort."""
    borrados = 0
    ahora = time.time()

    def viejos(pattern, dias, vacios=False):
        n = 0
        for ruta in glob.glob(pattern):
            try:
                if vacios and os.path.getsize(ruta) > 0:
                    continue
                if ahora - os.path.getmtime(ruta) > dias * 86400:
                    os.remove(ruta)
                    n += 1
            except OSError:
                continue
        return n

    # backups: keep the last 10
    try:
        bks = sorted(g for g in glob.glob(
            os.path.join(config.BASE, "hooks", "backups", "*")) if os.path.isdir(g))
        for p in bks[:-10]:
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            borrados += 1
    except OSError:
        pass
    borrados += viejos(os.path.join(config.LOG_DIR, "payload-*.json"), 2)
    borrados += viejos(os.path.join(config.LOG_DIR, "proto-stderr-*.log"), 1,
                       vacios=True)
    borrados += viejos(os.path.join(config.STATE_DIR, "bot-q", "*.json"), 0.1)
    borrados += viejos(os.path.join(config.stop_dir(), "*"), 0.5)
    borrados += viejos(os.path.join(config.STATE_DIR, "turn-*.marker"), 1)
    borrados += viejos(os.path.join(config.STATE_DIR, "cot-*.json"), 2)  # F40
    borrados += _rollout_viejos(ahora)
    _log_rotate(os.path.join(config.LOG_DIR, "errors.log"))
    _log_rotate(os.path.join(config.LOG_DIR, "orphan.log"))
    borrados += _logs_rotados_gordos()
    if borrados:
        pwlog("housekeeping: %d artefactos purgados" % borrados)


def _rollout_viejos(ahora):
    """F9 — transcripts of sessions the bot no longer references
    (rotated/archived): every rotation leaves the model-io-<sid>.jsonl
    behind and it grows with no ceiling (44MB measured per rotation).
    Those older than `rollout_retain_dias` (default 7, 0 = off) are
    purged unless they are: the active session (bot-session.json), one
    with an active turn (bot-active.json) or one with a live worker
    (bot-workers.json). The archived session is still reachable in the
    app; losing the transcript only means its resume no longer replays
    the complete history."""
    try:
        retener = float(load_config().get("rollout_retain_dias"))
    except (TypeError, ValueError):
        retener = 7.0
    if retener <= 0:
        return 0
    vivos = set()
    for nombre, por_clave in ((config.ARCH_SESION, False),
                              ("bot-active.json", True),
                              (config.ARCH_WORKERS, True)):
        try:
            with open(os.path.join(config.STATE_DIR, nombre)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        if por_clave:
            vivos.update(k for k in d
                         if isinstance(k, str) and k.startswith("ses"))
        elif isinstance(d.get("sid"), str):
            vivos.add(d["sid"])
    n = 0
    corte = ahora - retener * 86400
    for ruta in glob.glob(os.path.join(config.BASE, "cli", "rollout",
                                        "model-io-*.jsonl")):
        sid = os.path.basename(ruta)[len("model-io-"):-len(".jsonl")]
        if sid in vivos:
            continue
        try:
            if os.path.getmtime(ruta) > corte:
                continue
            os.remove(ruta)
            n += 1
        except OSError:
            continue
    return n
