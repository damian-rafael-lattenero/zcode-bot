"""Telegram HTTP (sync) — canonical bodies (F1e consolidation: the
gateway defined these same functions and shadowed this module; ONE
living body). The async client for the webhook lives in http_client.py.

Paths and config are read BY ATTRIBUTE (config.API / load_config from
zcode_bot.config) at call-time: fixture-aware in tests. `send` uses the
`cfg` it receives (this module's old copy ignored it and re-read
config 3 times — divergence classified in the 15/10 audit).
"""
import json
import time
import urllib.request

from zcode_bot import config
from zcode_bot import limits
from zcode_bot import audit
from zcode_bot.config import configured
from zcode_bot.utils.formatting import html_truncate, number_lines
from zcode_bot.utils.logging import log
from zcode_bot.utils.retry import retry_call
from zcode_bot.telegram.circuit_breaker import (
    CircuitBreakerConfig, CircuitBreakerOpen, SyncCircuitBreaker)


def tg_api(cfg, method, params=None, timeout=35):
    """Call to the Telegram Bot API."""
    token = cfg.get("token") or ""
    url = f"{config.API}/bot{token}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# F8 (15:47 incident): live streaming of a heavy turn saturated the
# bot's rate limit and blind retries KEPT the throttle alive (429s
# back-to-back for minutes, lost boxes). Since then: the first 429 sets
# the window Telegram asks for (parameters.retry_after) and every
# best-effort sender BACKS OFF until it passes — hammering more
# extends the punishment. `send` (the final reply) does truly wait.
_cooldown_until = [0.0]
_sleep = time.sleep

# F79e — the sync transport's breaker: 5 consecutive failed sends open
# it (sends go straight to the outbox, no hammering); getUpdates is the
# half-open probe; 2 successes close it.
_BREAKER_OPEN_S = 30.0   # OPEN lasts this long; then getUpdates probes
BREAKER = SyncCircuitBreaker("tg-sync",
                              CircuitBreakerConfig(failure_threshold=5,
                                                   timeout=_BREAKER_OPEN_S))


def breaker_state():
    """For the health line (healer) and tests."""
    return BREAKER.state


def _retry_after(exc):
    """Seconds the 429 asks to wait (body parameters.retry_after), or
    None if it isn't a 429. HTTPError carries the body in exc.read()."""
    if "429" not in str(exc) and "Too Many" not in str(exc):
        return None
    try:
        d = json.loads(exc.read().decode()) or {}
        return max(int((d.get("parameters") or {}).get("retry_after") or 0), 1)
    except Exception:  # noqa: BLE001 - silent-by-design: 429 without a readable body: conservative default
        return 5


def _message_id(r):
    """message_id of a sendMessage response (None if absent).
    bool is a subclass of int: True is NOT an id."""
    try:
        mid = (r.get("result") or {}).get("message_id")
    except AttributeError:
        return None
    return mid if isinstance(mid, int) and not isinstance(mid, bool) else None


def send(cfg, text, loud=None, markup=None, a_outbox=True):
    """Sends a message with retries; returns the message_id if it went
    out (True if Telegram didn't include it — always truthy), False if
    not. Never raises. The id is what allows EDITING that message later
    (the growing CoT keyboard): returning a bare True left button boxes
    stuck at «CoT 1» — editMessageText(message_id=True) fails silently.

    loud=None → notifies only if loud_all is on; loud=True/False forces.
    markup → reply_markup dict (inline buttons), e.g. {"inline_keyboard": [...]}.
    a_outbox=False → no re-enqueue on failure (outbox_flush already has it)."""
    if not configured(cfg):
        return False
    if loud is None:
        loud = bool(cfg.get("loud_all"))  # F34c divergence: it used to default to False
    params = {
        "chat_id": cfg["chat_id"],
        "text": html_truncate(text, limits.CH_REPLY),
        "parse_mode": "HTML",
        "disable_notification": not loud,
    }
    if markup:
        params["reply_markup"] = json.dumps(markup)

    def _attempt():
        return _message_id(tg_api(cfg, "sendMessage", params)) or True

    def _tg_retryable(exc):
        # 4xx (but not 429): retrying a bad request is hammering with
        # extra steps — fail fast, the outbox path decides
        code = getattr(exc, "code", None)
        return not (isinstance(code, int) and 400 <= code < 500
                    and code != 429)

    def _attempt_log(n_attempt, exc, delay):
        if _retry_after(exc) is None:   # the 429 wait is not an error
            log("errors", "sendMessage failed (attempt %d/3): %s"
                % (n_attempt, exc))

    def _set_cooldown(ra):
        _cooldown_until[0] = time.time() + ra

    try:
        return BREAKER.call(
            lambda: retry_call(
                _attempt,
                backoffs=(0, 1, 3),  # one-off network resets don't lose the notification
                sleep=_sleep,
                should_retry=_tg_retryable,
                retry_after_of=_retry_after,
                on_retry_after=_set_cooldown,
                on_attempt_fail=_attempt_log))
    except CircuitBreakerOpen:
        # F79e: Telegram/network is down and the breaker says so — the
        # outbox holds the message; getUpdates (the cheapest call) is
        # the half-open probe and the poller flushes when it closes
        log("errors", "sendMessage: breaker OPEN — to the outbox")
    except Exception as exc:  # noqa: BLE001 - a hook must never break the session
        if _retry_after(exc) is not None:
            _cooldown_until[0] = time.time() + _retry_after(exc)
        log("errors", "sendMessage failed (final): %s" % exc)
    if a_outbox:
        # F30: FINAL send failure → global log (the auditor correlates
        # bursts: the 15:47 429/deadlock incident would have alerted here)
        audit.record(None, "send", "sendMessage failed, going to the outbox")
        _outbox_put(text)  # network down: persist and let the poller retry
    return False


def _live_send(cfg, text):
    """sendMessage that returns the message_id (for the live placeholder)."""
    if time.time() < _cooldown_until[0]:
        return None  # throttled: the box is born dead — never critical
    try:
        r = tg_api(cfg, "sendMessage",
                   {"chat_id": cfg["chat_id"],
                    "text": html_truncate(text, limits.CH_REPLY),
                    "parse_mode": "HTML", "disable_notification": True})
        return _message_id(r)
    except Exception as exc:  # noqa: BLE001 - one-line by design: live-view I/O, F79e adds retry+breaker
        log("errors", "live send failed: %s" % exc)
        return None


def _live_edit(cfg, msg_id, text, markup=None):
    """Best-effort editMessageText: the live view is optional, a failure
    never breaks anything. On 429 it sets the global cooldown and does NOT
    retry: while it lasts, edits skip themselves (streaming degrades,
    doesn't hammer). markup: inline keyboard alongside the text (F41: the
    live CoT keyboard GROWS by editing, without message spam)."""
    if time.time() < _cooldown_until[0]:
        return False
    try:
        params = {"chat_id": cfg["chat_id"], "message_id": msg_id,
                  "text": html_truncate(text, limits.CH_REPLY),
                  "parse_mode": "HTML"}
        if markup is not None:
            params["reply_markup"] = markup
        tg_api(cfg, "editMessageText", params)
        return True
    except Exception as exc:  # noqa: BLE001 - one-line by design: live-view I/O, F79e adds retry+breaker
        log("errors", "live edit failed: %s" % exc)
        ra = _retry_after(exc)
        if ra is not None:
            _cooldown_until[0] = time.time() + ra
        return False


def _delete(cfg, msg_id):
    """Best-effort deleteMessage (the CoT reader window's ✕). Never
    raises; on 429 sets the global cooldown like the live edits."""
    if time.time() < _cooldown_until[0]:
        return False
    try:
        tg_api(cfg, "deleteMessage",
               {"chat_id": cfg["chat_id"], "message_id": msg_id})
        return True
    except Exception as exc:  # noqa: BLE001 - one-line by design: best-effort delete
        log("errors", "delete failed: %s" % exc)
        return False


def _multipart_body(fields, fname, content):
    """multipart/form-data body (pure stdlib) for sendDocument."""
    import uuid
    b = "----zcb" + uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
                 "%s\r\n" % (b, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; "
             "filename=\"%s\"\r\nContent-Type: text/plain\r\n\r\n"
             % (b, fname)).encode()
    body += content + ("\r\n--%s--\r\n" % b).encode()
    return body, "multipart/form-data; boundary=%s" % b


def send_document(cfg, filename, text, caption=""):
    """Sends a .txt as a Telegram document: a turn's full CoT doesn't fit
    in 4096-char messages — the document viewer scrolls it whole
    (sendDocument, core.telegram.org/bots/api). F55: the content goes out
    through number_lines — every file shown in the chat carries its line
    numbers, whatever the producer."""
    if not configured(cfg):
        return False
    try:
        body, ctype = _multipart_body(
            {"chat_id": str(cfg["chat_id"]), "caption": caption[:900]},
            filename, number_lines(text).encode("utf-8"))
        req = urllib.request.Request(
            "%s/bot%s/sendDocument" % (config.API, cfg.get("token") or ""),
            data=body, headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=90) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as exc:  # noqa: BLE001 - never break the turn
        log("errors", "sendDocument: %s" % exc)
        return False


def _outbox_store():
    """The outbox's AtomicStore — path resolved AT CALL TIME (fixture-aware)."""
    from zcode_bot.state import AtomicStore
    import os
    return AtomicStore(os.path.join(config.STATE_DIR, "outbox.json"))


def _outbox_put(text):
    """Enqueues a message for when the network comes back. Multi-process
    (hooks + poller write): read-modify-write under flock."""
    def add(box):
        box = box if isinstance(box, list) else []
        box.append({"ts": int(time.time()), "text": text[:limits.CH_REPLY]})
        return box[-20:]
    try:
        _outbox_store().update_cross_process(add, [])
    except OSError as e:
        # flock deadline expired (pathological): the message is lost from
        # the outbox but send already tried 3 times — degrade with a log
        log("errors", "outbox_put: %s" % e)


def outbox_flush(cfg):
    """Resends what piled up once the network came back. Returns the count sent.

    15:47 incident: this used to send INSIDE the flock and the failed send
    re-enqueued with ANOTHER flock on the same file → self-deadlock (flock
    isn't reentrant across fds) → poller frozen 30+ min. Now the lock only
    covers the SWAP (fast): sends go OUTSIDE, failed ones go back to the
    end of the queue; a_outbox=False avoids the nested re-enqueue (they're
    already in the queue: put_back keeps them)."""
    store = _outbox_store()
    taken = [None]

    def swap(box):
        taken[0] = box if isinstance(box, list) else []
        return []

    try:
        # cross-process (audit 20/10): update() only takes the in-process
        # RLock — a hook's flocked append could be clobbered by this
        # replace. The swap stays fast (sends outside).
        store.update_cross_process(swap, [])
    except OSError as e:
        log("errors", "outbox_flush swap: %s" % e)
        return 0
    ok = 0
    remaining = []
    for item in taken[0]:
        if send(cfg, item.get("text", ""), loud=False, a_outbox=False):
            ok += 1
        else:
            remaining.append(item)
    if remaining:
        def put_back(box):
            box = box if isinstance(box, list) else []
            return remaining + box
        try:
            store.update_cross_process(put_back, [])
        except OSError as e:
            log("errors", "outbox_flush put_back (%d perdidos): %s"
                % (len(remaining), e))
    return ok
