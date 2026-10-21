"""commands.dispatch — the 2_bot dispatch (F4i).

wait_reply (waiting for the owner's reply), the Command Registry table
with the ~25 handlers and the two entry points (handle_command for
"/commands", handle_free_text for free text → active conversation).

F9 (ADR-012 debt close-out): the handlers live by domain in
handlers_tasks (conversations/queue), handlers_info (reports/menu)
and handlers_system (control/model/webhook) — moved VERBATIM, the
table here only registers them. This module keeps wait_reply (the
wait shared with hooks) and the re-exported surface the gateway
consumes. Transport VIA ATTRIBUTE (uhttp.*).
"""
import os
import threading
import time
from typing import Any, Dict, Tuple


from zcode_bot import commands as zc_commands
from zcode_bot import config
from zcode_bot.core import WaitReply
from zcode_bot.updates import (drain_updates, save_offset, busy_path,
                               fetch_updates, from_owner)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.validation import classify
from zcode_bot.utils.logging import log

# handlers by domain (F9) — re-exported: the facade (gateway) imports
# these names from here, same as since F4i
from zcode_bot.commands.registry import CommandSpec
from zcode_bot.commands.handlers_tasks import (  # noqa: F401
    task_select, _task_select_sid, handle_free_text, _cmd_retry, _cmd_stop,
    _cmd_task, _cmd_all_tasks, _cmd_queue, _queue_mutate, _cmd_qgo,
    _cmd_qdel, _cmd_run)
from zcode_bot.commands.handlers_info import (  # noqa: F401
    register_bot_menu, _cmd_quota, _cmd_usage, _cmd_ctx, _cmd_compact,
    _cmd_all_compact, _cmd_status, _cmd_last, _cmd_cot, _cmd_help,
    _cmd_diff, _cmd_cotb)
from zcode_bot.commands.handlers_system import (  # noqa: F401
    _cmd_start, _cmd_deploy, _cmd_nuclear, _cmd_ping, _cmd_model,
    _cmd_thinking, _cmd_mode, _cmd_fork, _cmd_api, _cmd_set_webhook,
    _cmd_delete_webhook, _cmd_webhook_info)


def wait_reply(cfg, hint):
    """Waits for a NEW reply via Telegram up to wait_timeout_s.

    `hint` labels the wait ("pending approval", "pending question")
    in the error log, so a failing getUpdates says WHICH wait broke.

    Returns ("yes"|"no"|"text", text) or (None, None) if the time ran
    out. Messages starting with "/" are 2_bot commands: they are answered
    and the wait continues (they do not count as a reply).
    """
    deadline = time.time() + int(cfg.get("wait_timeout_s"))
    offset, stale = drain_updates(cfg)  # what was pending does not count as a reply
    save_offset(offset)
    os.makedirs(config.STATE_DIR, exist_ok=True)
    try:
        while time.time() < deadline:
            # flag for the command poller: do not consume getUpdates while we wait
            with open(busy_path(), "w") as f:
                f.write(str(time.time()))
            remaining = int(deadline - time.time())
            poll_timeout = max(1, min(25, remaining))
            try:
                offset, batch, _ = fetch_updates(cfg, offset, poll_timeout)
                save_offset(offset)
            except Exception as exc:  # noqa: BLE001
                log("errors", f"getUpdates (wait, {hint}) failed: {exc}")
                # No sleep(3): the long-poll timeout already did the waiting
                continue
            for msg in batch:
                if not from_owner(cfg, msg):
                    continue
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    reply = handle_command(cfg, text)
                    if reply is not None:
                        if isinstance(reply, tuple):
                            uhttp.send(cfg, reply[0], loud=False, markup=reply[1])
                        else:
                            uhttp.send(cfg, reply, loud=False)
                        continue
                kind = classify(text)
                return WaitReply(kind if kind in ("yes", "no") else "text", text)
            if not batch:
                # No sleep(0.5): the long-poll timeout=25 already blocks on the server
                pass
        return WaitReply(None, None)
    finally:
        try:
            os.remove(busy_path())
        except OSError:
            pass


REG = zc_commands.CommandRegistry()  # table-driven dispatch (Command Registry)

# R6 — THE command table: REG, SLOW_CMDS and the Telegram menu come out
# of here (the visible ones go FIRST and in menu order; registration
# order is irrelevant for dispatch). /help is curated text (lines
# grouped by hand) but test_help_cubre_specs requires that every
# visible command appear in it. Empty menu_desc = hidden from the menu
# (internal callbacks: qgo/qdel; bootstrap: start; admin: webhook/deploy).
SPECS = [
    CommandSpec(("task",), _cmd_task,
                menu_desc="conversations with Project · /task n picks the active one"),
    CommandSpec(("all_tasks", "alltasks", "tasks"), _cmd_all_tasks,
                menu_desc="all conversations, loose ones included"),
    CommandSpec(("model",), _cmd_model,
                menu_desc="model of the active conversation (buttons)"),
    CommandSpec(("thinking",), _cmd_thinking,
                menu_desc="reasoning level: max · high · low"),
    CommandSpec(("mode",), _cmd_mode,
                menu_desc="Plan mode 🗺 · Full access 🔓 (buttons)"),
    CommandSpec(("status",), _cmd_status,
                menu_desc="what is running + active conversation"),
    CommandSpec(("diff",), _cmd_diff,
                menu_desc="work in progress of the active workspace · /diff log = progress · /diff commit"),
    CommandSpec(("queue", "cola"), _cmd_queue,  # noqa: spanish — /cola legacy alias, permanent bilingual command
                menu_desc="queued messages (launch now / discard)"),
    CommandSpec(("retry",), _cmd_retry,
                menu_desc="relaunch the last prompt (after a failure)"),
    CommandSpec(("ctx", "contexto"), _cmd_ctx,
                menu_desc="context used by each task", slow=True),
    CommandSpec(("compact",), _cmd_compact,
                menu_desc="pick a task to compact (Projects)", slow=True),
    CommandSpec(("all_compact", "allcompact"), _cmd_all_compact,
                menu_desc="same, loose Tasks-group ones included",
                slow=True),
    CommandSpec(("run",), _cmd_run,
                menu_desc="new task and send prompt (/run project prompt)"),
    CommandSpec(("stop", "all_stop", "allstop"), _cmd_stop,
                menu_desc="stops the run and CLEARS its message queue",
                slow=True),
    CommandSpec(("nuclear", "nuke"), _cmd_nuclear,
                menu_desc="emergency: stops EVERYTHING (Mac included) and restarts"),
    CommandSpec(("quota",), _cmd_quota, menu_desc="coding plan credits",
                slow=True),
    CommandSpec(("usage", "uso"), _cmd_usage,
                menu_desc="today's usage by session", slow=True),
    CommandSpec(("last",), _cmd_last,
                menu_desc="resend the last full reply"),
    CommandSpec(("cot", "trace"), _cmd_cot,
                menu_desc="step by step + turn reasoning (document)"),
    CommandSpec(("help",), _cmd_help, menu_desc="all commands"),
    # — hidden from the menu —
    CommandSpec(("start",), _cmd_start),
    CommandSpec(("deploy",), _cmd_deploy),
    CommandSpec(("cotb",), _cmd_cotb),
    CommandSpec(("qgo",), _cmd_qgo),   # internal callbacks of /queue
    CommandSpec(("qdel",), _cmd_qdel),
    CommandSpec(("fork",), _cmd_fork),
    CommandSpec(("api",), _cmd_api),
    CommandSpec(("ping",), _cmd_ping),
    CommandSpec(("set_webhook", "setwebhook"), _cmd_set_webhook),
    CommandSpec(("delete_webhook", "deletewebhook"), _cmd_delete_webhook),
    CommandSpec(("webhook_info", "webhookinfo"), _cmd_webhook_info),
]
for _spec in SPECS:
    REG.register(*_spec.names)(_spec.fn)

# F37b — slow commands: they run in a thread (see handle_command).
# Derived from the specs: name and ALIAS of every slow command.
SLOW_ASYNC = True
SLOW_CMDS = {n for s in SPECS if s.slow for n in s.names}
ACK_SLOW = "⏳ In background — the result arrives in a separate message."

# F38: slow commands IN FLIGHT — since F37b they run in a thread and
# were invisible to /status (Damian: "make status tell me what is
# running right now"). base → (t0, command text).
INFLIGHT: Dict[str, Tuple[float, str]] = {}
_INFLIGHT_LOCK = threading.Lock()


def inflight_snapshot():
    """Stable copy of what is running: [(base, t0, text)]."""
    with _INFLIGHT_LOCK:
        return [(b, v[0], v[1]) for b, v in INFLIGHT.items()]


def _run_slow(cfg, text, base):
    with _INFLIGHT_LOCK:
        INFLIGHT[base] = (time.time(), text)
    try:
        r = REG.dispatch(cfg, text)
        if isinstance(r, tuple):
            uhttp.send(cfg, r[0], loud=False, markup=r[1])
        elif r is not None:
            uhttp.send(cfg, r, loud=False)
    except Exception as exc:  # noqa: BLE001 - the thread reports, it does not die
        log("errors", "slow-cmd /%s: %s" % (base, exc))
        uhttp.send(cfg, "⚠️ Error in /%s: %s" % (base, esc(str(exc)[:120])),
                   loud=True)
    finally:
        with _INFLIGHT_LOCK:
            INFLIGHT.pop(base, None)


def handle_command(cfg, text):
    """2_bot commands (dispatch via Command Registry). Returns the
    reply (HTML, or (text, markup)) or None if it is not a command.

    F37b — Damian's directive: EVERYTHING coming in from the phone is
    async, the poller NEVER waits for what is in flight. Commands that
    touch the protocol or grace (compact: minutes; ctx/usage/quota:
    roundtrips to the runtime; stop: 4s grace) run in a thread and
    their result travels as a separate message; only the instant ack
    goes out here. In tests SLOW_ASYNC=False (support.fresh):
    everything synchronous as always."""
    base = ""
    if text.startswith("/"):
        head = text[1:].split()
        base = head[0].split("@")[0].lower() if head else ""
    if SLOW_ASYNC and base in SLOW_CMDS:
        threading.Thread(target=_run_slow, args=(cfg, text, base),
                         daemon=True, name="cmd-%s" % base).start()
        return ACK_SLOW
    return REG.dispatch(cfg, text)


def known_command(text):
    """F48a — is «text» a registered command? The gateway asks this
    BEFORE declaring «Comando desconocido»: REG.dispatch returns None
    both when there is no handler and when the handler chose not to
    reply (/cotb edited in place, /last, …) — with the None alone,
    every tap of a little CoT box got the banner for free (Damian,
    20/10 20:51)."""
    return REG.lookup(text)[0] is not None
