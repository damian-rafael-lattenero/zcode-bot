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
from zcode_bot.telegram.circuit_breaker import CircuitBreakerOpen
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
BUSY_FRESH_S = 360


# A .log.1 has already been rotated OUT of the way (diagnostics for a
# closed incident); if it also weighs more than this, it is not a log,
# it is a flood — the 294MB errors.log.1 of the 19/10 incident (3.1M
# identical tracebacks in ~5min) had no reason to outlive its own
# housekeeping.
LOG_ROTATED_CAP_BYTES = 50 * 1024 * 1024


def _rotate_big_logs():
    """Purges *.log.1 files over the cap: write-time rotation (log())
    bounds the live flood, but the .1 it leaves behind cannot be left
    unbounded either."""
    n = 0
    for path in glob.glob(os.path.join(config.LOG_DIR, "*.log.1")):
        try:
            if os.path.getsize(path) > LOG_ROTATED_CAP_BYTES:
                os.remove(path)
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
        max_age = BUSY_FRESH_S
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


from zcode_bot.commands.callbacks import callback_text  # noqa: E402

# R5: the prefix→command table lives in commands/callbacks.py (a single
# body); the alias keeps the historical name that the gateway and the
# test suite import from here.
_cb_text = callback_text


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
    except Exception:  # noqa: BLE001  # silent-by-design: deliberate degradation
        pass


def typing(cfg, seconds=5):
    """F23 — sendChatAction 'typing': kills the mute window between the
    owner's message and the worker's placeholder. Best-effort."""
    try:
        uhttp.tg_api(cfg, "sendChatAction",
               {"chat_id": cfg["chat_id"], "action": "typing"})
    except Exception:  # noqa: BLE001  # silent-by-design: deliberate degradation
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
        # F59 — the ct:/cr: taps edit the TAPPED message itself: its
        # message_id travels as a self-describing ph= token (it can
        # never collide with /cotb's positional part argument). F63:
        # the agent's ag:/agr: taps are the same in-place contract
        if data.startswith(("ct:", "cr:", "ag:", "agr:")) \
                and pseudo.get("message_id"):
            pseudo["text"] += " ph=%d" % pseudo["message_id"]
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
    # F79e: getUpdates rides the sync breaker — when Telegram/network
    # is down it fails FAST (no 35s timeout burned per 3s cycle) and it
    # IS the half-open probe: the cheapest call decides when the flow
    # reopens (2 clean polls close it, the outbox flushes right after)
    try:
        res = uhttp.BREAKER.call(
            uhttp.tg_api, cfg, "getUpdates",
            {"offset": offset, "timeout": timeout, "limit": 100},
            timeout=timeout + 15)
    except CircuitBreakerOpen:
        # policy, not error: the breaker is OPEN — an empty cycle (the
        # breaker's own timeout reopens the probe; /handshake shows it)
        return offset, [], 0
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
            log("errors", f"getUpdates failed: {exc}")
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
        log("errors", "from_owner: config without owner_user_id — update "
                      "discarded (fail-closed; register with "
                      "`zcode-telegram.py register`)")
        return False
    return msg.get("from", {}).get("id") == owner


def _hq_pending():
    return _questions().any_pending()


def _reload_now_path():
    return os.path.join(config.STATE_DIR, config.RELOAD_FILE)


def housekeeping():
    """F24 — purges what piles up with no ceiling (13/10 audit:
    23 backups never purged, 964 payload-*.json with plaintext prompts,
    178 empty proto-stderr files, orphan q-*.json, old markers). Runs
    ONCE at poller startup. All best-effort."""
    deleted = 0
    now = time.time()

    def _old_files(pattern, days, empty=False):
        n = 0
        for path in glob.glob(pattern):
            try:
                if empty and os.path.getsize(path) > 0:
                    continue
                if now - os.path.getmtime(path) > days * 86400:
                    os.remove(path)
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
            deleted += 1
    except OSError:
        pass  # silent-by-design: deliberate degradation
    deleted += _old_files(os.path.join(config.LOG_DIR, "payload-*.json"), 2)
    deleted += _old_files(os.path.join(config.LOG_DIR, "proto-stderr-*.log"), 1,
                          empty=True)
    deleted += _old_files(os.path.join(config.STATE_DIR, "bot-q", "*.json"), 0.1)
    deleted += _old_files(os.path.join(config.stop_dir(), "*"), 0.5)
    deleted += _old_files(os.path.join(config.STATE_DIR, "turn-*.marker"), 1)
    deleted += _old_files(os.path.join(config.STATE_DIR, "cot-*.json"), 2)  # F40
    # P6: the audit log self-caps at CAP entries, so the mtime only goes
    # stale when the bot stopped writing — both names are purged then
    # (the legacy bot-auditoria.json stops being written at the deploy)
    deleted += _old_files(os.path.join(config.STATE_DIR, config.AUDIT_FILE), 2)
    deleted += _old_files(os.path.join(config.STATE_DIR,
                                       config.AUDIT_FILE_LEGACY), 2)
    deleted += _purge_old_rollouts(now)
    _log_rotate(os.path.join(config.LOG_DIR, "errors.log"))
    _log_rotate(os.path.join(config.LOG_DIR, "orphan.log"))
    deleted += _rotate_big_logs()
    if deleted:
        pwlog("housekeeping: %d artifacts purged" % deleted)


def _purge_old_rollouts(now):
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
        retain = float(load_config().get("rollout_retain_days"))
    except (TypeError, ValueError):
        retain = 7.0
    if retain <= 0:
        return 0
    live = set()
    for name, by_key in ((config.SESSION_FILE, False),
                         ("bot-active.json", True),
                         (config.WORKERS_FILE, True)):
        try:
            with open(os.path.join(config.STATE_DIR, name)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        if by_key:
            live.update(k for k in d
                        if isinstance(k, str) and k.startswith("ses"))
        elif isinstance(d.get("sid"), str):
            live.add(d["sid"])
    n = 0
    cutoff = now - retain * 86400
    for path in glob.glob(os.path.join(config.BASE, "cli", "rollout",
                                       "model-io-*.jsonl")):
        sid = os.path.basename(path)[len("model-io-"):-len(".jsonl")]
        if sid in live:
            continue
        try:
            if os.path.getmtime(path) > cutoff:
                continue
            os.remove(path)
            n += 1
        except OSError:
            continue
    return n