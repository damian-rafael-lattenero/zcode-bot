"""Bot logging — the canonical engine (F79b) over the F1e bodies.

Three channels, three contracts:
- pwlog: the FORENSIC step log (prompt-worker.log, kb events). Human
  timeline of the bot's steps — format frozen (3 incidents reconstructed
  over it), NEVER route it through the engine below.
- log/log_exc: the EVENT channel (LOG_DIR/<kind>.log) — now leveled
  stdlib logging through ONE BotFormatter: every record answers who
  executed (logger name + component), who called (stacklevel), when
  (ms + TZ), what failed (the message), why (cause= + the full,
  indented stack — never truncated), with the ambient context
  (bind_context: sid/turn/component) riding contextvars.
- hb: the per-cycle heartbeat (F79a) — mtime touches in state/.

Paths by attribute (config.LOG_DIR / config.BASE): fixture-aware in
tests (the handler cache re-opens when LOG_DIR moves).
"""
import contextvars
import datetime
import json
import logging
import logging.handlers
import os
import time
import traceback

from zcode_bot import config

# ------------------------------------------------------- ambient context

_CTX_SID = contextvars.ContextVar("bot_sid", default="")
_CTX_TURN = contextvars.ContextVar("bot_turn", default="")
_CTX_COMPONENT = contextvars.ContextVar("bot_component", default="")


def bind_context(sid="", turn="", component=""):
    """F79b — bind the ambient context ONCE (spawn/attach/watcher entry):
    every record from this thread/task carries it. ContextVars copy on
    thread start, so a worker's threads inherit the turn they serve.
    Returns the previous values as a dict (for tests to restore)."""
    prev = {"sid": _CTX_SID.get(), "turn": _CTX_TURN.get(),
            "component": _CTX_COMPONENT.get()}
    _CTX_SID.set(sid)
    _CTX_TURN.set(turn)
    _CTX_COMPONENT.set(component)
    return prev


def context_now():
    """The bound context as a dict (readers: /status, the healer)."""
    return {"sid": _CTX_SID.get(), "turn": _CTX_TURN.get(),
            "component": _CTX_COMPONENT.get()}


# ----------------------------------------------------------- BotErrors

class BotError(Exception):
    """F79b — the domain hierarchy. Carries its own context so the
    formatter can show WHAT failed without losing the original cause
    (always raise ... from the caught exception)."""
    component = "bot"

    def __init__(self, msg, **ctx):
        super().__init__(msg)
        self.bot_ctx = ctx


class SendError(BotError):
    component = "send"


class StoreError(BotError):
    component = "store"


class AttachError(BotError):
    component = "attach"


# --------------------------------------------------------- the formatter

def _tz_pretty():
    off = datetime.datetime.now().astimezone().strftime("%z")  # -0300
    return off[:3] if len(off) == 5 and off.endswith("00") else off


class BotFormatter(logging.Formatter):
    """The ONE format (F79b):

    [2026-10-27 14:25:48.123 -03] ERROR zcode_bot.errors component=cot-live sid=b14e2541 turn=7ef4f6ee cause=KeyError: 'ph'
    the worker died sealing the box
        Traceback (most recent call last):
          ...

    Header: when (ms + TZ), level, logger, the ambient context (only
    non-empty keys), BotError context, and cause= (the exception's own
    line) when a stack rides the record. Body: the message. Stack:
    indented 4 — greppable, one record = one block."""

    def format(self, record):
        when = datetime.datetime.fromtimestamp(record.created)
        stamp = "%s.%03d %s" % (when.strftime("%Y-%m-%d %H:%M:%S"),
                                record.msecs, _tz_pretty())
        keys = []
        ctx = context_now()
        if ctx.get("component"):
            keys.append("component=%s" % ctx["component"])
        if ctx.get("sid"):
            keys.append("sid=%s" % ctx["sid"])
        if ctx.get("turn"):
            keys.append("turn=%s" % ctx["turn"])
        bot_err = None
        if isinstance(record.msg, BotError):
            bot_err = record.msg
        elif record.exc_info and isinstance(record.exc_info[1], BotError):
            bot_err = record.exc_info[1]   # the wall: raise...from chain
        if bot_err is not None and getattr(bot_err, "bot_ctx", None):
            keys.append("bot_ctx=%s" % json.dumps(
                bot_err.bot_ctx, ensure_ascii=False, sort_keys=True))
        if record.exc_info and record.exc_info[1] is not None:
            cause = traceback.format_exception_only(
                *record.exc_info[:2])[-1].strip().splitlines()[0]
            # 3.13 qualifies non-builtin names with the module
            # (zcode_bot.utils.logging.AttachError) — the bare name is
            # the readable cause, the stack below keeps the full path
            if ": " in cause:
                name, rest = cause.split(": ", 1)
                cause = "%s: %s" % (name.rsplit(".", 1)[-1], rest)
            keys.append("cause=%s" % cause)
        head = "[%s] %s %s" % (stamp, record.levelname, record.name)
        if keys:
            head += " " + " ".join(keys)
        body = record.getMessage()
        out = head + "\n" + body
        if record.exc_info:
            stack = self.formatException(record.exc_info).rstrip("\n")
            out += "\n" + "\n".join("    " + ln for ln in stack.splitlines())
        return out


# ------------------------------------------------------- handlers/engine

_ROT_BYTES = 5 * 1024 * 1024   # one generation (.1): diagnostics, not an archive
_HANDLERS = {}                 # type: dict[str, tuple[str, logging.Handler]]
# kind → (path, handler); reopens on LOG_DIR move


def _handler_for(kind):
    path = os.path.join(config.LOG_DIR, "%s.log" % kind)
    got = _HANDLERS.get(kind)
    if got and got[0] == path:
        return got[1]
    if got:
        try:
            got[1].close()
        except Exception:  # noqa: BLE001 — silent-by-design: swapping handlers
            pass
    os.makedirs(config.LOG_DIR, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(
        path, maxBytes=_ROT_BYTES, backupCount=1, encoding="utf-8")
    h.setFormatter(BotFormatter())
    h.setLevel(logging.DEBUG)
    _HANDLERS[kind] = (path, h)
    return h


def get_logger(kind="errors"):
    """zcode_bot.<kind> — one logger per channel file, no propagation
    (root has no handlers here; propagate would double-write). The
    logger is a stdlib singleton that OUTLIVES fixtures: if OUR file
    handler points at another LOG_DIR (tests rebind per test — and
    pytest hangs its own capture handlers on the same logger, so
    "any handler" is NOT "ours"), it is swapped — a stale handler would
    keep writing into a deleted tree."""
    lg = logging.getLogger("zcode_bot.%s" % kind)
    want = os.path.abspath(os.path.join(config.LOG_DIR, "%s.log" % kind))
    ok = False
    for h in lg.handlers[:]:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            if h.baseFilename == want:
                ok = True
            else:
                lg.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001 — silent-by-design: swap
                    pass
    if not ok:
        lg.addHandler(_handler_for(kind))
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    return lg


def log(kind, msg):
    """The event channel (F79b engine under the F1e signature): INFO,
    caller-named (stacklevel), ambient context automatic."""
    try:
        get_logger(kind).info(msg, stacklevel=2)
    except Exception:  # noqa: BLE001 — silent-by-design: logging never kills
        pass


def log_exc(kind, msg):
    """The exception wall (F79a/F79b): ERROR + the FULL, indented,
    never-truncated stack. Call inside the except block. Without an
    active exception it degrades to log() (exc_info=True with no live
    exception would print 'NoneType: None')."""
    import sys
    if sys.exc_info()[2] is None:
        log(kind, msg)
        return
    try:
        get_logger(kind).error(msg, exc_info=True, stacklevel=3)
    except Exception:  # noqa: BLE001 — silent-by-design: logging never kills
        pass


def _reset_handlers():
    """Tests only: drop the handler cache AND detach from the singleton
    loggers (they outlive any fixture)."""
    for kind in list(_HANDLERS):
        lg = logging.getLogger("zcode_bot.%s" % kind)
        for h in lg.handlers[:]:
            lg.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001 — silent-by-design: teardown
                pass
        _HANDLERS.pop(kind, None)
    _HANDLERS.clear()


# ------------------------------------------------------------- hb + pwlog

def hb(name):
    """Per-cycle heartbeat (F79a): touch state/hb-<name>. The mtime IS
    the beat — the healer distinguishes an idle-but-alive loop (fresh
    mtime) from a dead one (stale) by lstat alone. Best-effort: a
    heartbeat failure must never kill the loop it measures."""
    try:
        p = os.path.join(config.STATE_DIR, "hb-%s" % name)
        with open(p, "a"):
            os.utime(p, None)
    except OSError:
        pass  # silent-by-design: diagnostics beat, never fatal


def install_death_diagnostics():
    """F79a — nothing may die silently anymore. The 27/10 14:25:48
    incident: a worker thread/watcher died inside a live process and
    left NO trace anywhere (prompt-worker.log froze mid-turn, the CoT
    cache kept growing, no traceback in any log). Two nets, both
    best-effort, installed at EVERY entrypoint (they all run through
    gateway.main(): the poller, the workers, every watcher):
    - faulthandler: interpreter-level faults (segfault, abort) get a
      native traceback dumped to stderr — the proto-stderr-*.log files
      the runtime already captures.
    - threading.excepthook: a thread that dies with an unhandled
      exception gets its full stack in errors.log, and the REST of the
      process keeps running (the default hook printed to stderr, which
      these detached processes don't have)."""
    try:
        import faulthandler
        faulthandler.enable()
    except Exception:  # noqa: BLE001 — silent-by-design: diagnostics bootstrap, never fatal
        pass

    def _thread_died(args):
        name = getattr(args.thread, "name", "?")
        log_exc("errors", "THREAD DIED: %s (the process lives on)" % name)

    import threading
    threading.excepthook = _thread_died


def log_payload(event, payload):
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(config.LOG_DIR, f"payload-{stamp}-{event}.json"),
                  "w") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    except (OSError, TypeError):
        pass  # silent-by-design: cache/state write, best-effort


def pwlog(*a):
    """Unified bot step log (worker + poller entries). F24: with a DATE
    (the 13/10 forensic reconstruction required crossing 3 sources to
    date lines) and size-based rotation. FORENSIC channel — its format
    is frozen; it never goes through the F79b engine."""
    try:
        p = os.path.join(config.BASE, "hooks", "logs", "prompt-worker.log")
        _log_rotate(p)
        with open(p, "a") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now()
                                   .strftime("%m-%d %H:%M:%S"),
                                   " ".join(str(x) for x in a)))
    except OSError:
        pass  # silent-by-design: cache/state write, best-effort


def _log_rotate(path, max_bytes=5 * 1024 * 1024):
    """>5MB → .1 (one generation: the log is diagnostics, not an archive)."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass  # silent-by-design: cache/state write, best-effort
