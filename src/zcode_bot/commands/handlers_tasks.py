"""commands.handlers_tasks — conversation and queue handlers (F9).

Moved VERBATIM from commands.dispatch (debt declared in ADR-012, F9
close-out): task_select/_task_select_sid, handle_free_text, _cmd_retry,
_cmd_stop, _cmd_task, _cmd_all_tasks, _cmd_queue, _queue_mutate,
_cmd_qgo and _cmd_qdel. The owner's prompt enters here and goes down
to workers._spawn_worker. Transport VIA ATTRIBUTE (uhttp.* through
the seams of each owning module).
"""
import os

from zcode_bot import config
from zcode_bot.commands import args
from zcode_bot.core import (active_bot_session, _load_last_prompt,
                            active_workspace_path, session_title_from_db,
                            set_active_bot_session, _store, _workers,
                            _questions, running_turns, _queue_peek,
                            _queue_sids_with_items)
from zcode_bot.workers import _spawn_worker, bot_stop, _queue_put, _queue_resume
from zcode_bot.reporting import (task_list_report, stop_list_report,
                                 _tasks_scope, _resolve_ws, _ctx_tasks)
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.config import load_config


def task_select(n, scope="projects"):
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    if n < 1 or n > len(tasks):
        return args.out_of_list_error("task", len(tasks))
    t = tasks[n - 1]
    return _task_select_sid(t["sid"], t["title"])


def _task_select_sid(sid, title=None):
    """Sets the active conversation by sessionId (the buttons use it: the
    numeric position can repeat across workspaces, the sid cannot)."""
    tasks = {t["sid"]: t for t in _ctx_tasks(30)}
    t = tasks.get(sid)
    if not t:
        return "⚠️ I couldn't find that conversation — send /task."
    prev = active_bot_session() or {}
    new = {"sid": t["sid"], "title": t["title"] or t["sid"][-8:],
           "ws": t["ws"]}
    # F22: the owner's config SURVIVES the switch (before, /model,
    # /thinking and /mode were lost when changing conversations).
    # F7+++: mode_set too — without it, every switch paid the 30s setMode
    # again
    for k in ("model", "mode", "mode_set"):
        if prev.get(k):
            new[k] = prev[k]
    set_active_bot_session(new)
    running = running_turns()
    extra = " (⚠️ a turn is running right now)" if t["sid"] in running else ""
    reply = ("🎯 Active conversation: «%s»%s\nAll text you send goes there "
             "(it runs headless — works with the screen locked)."
             % (esc(t["title"] or t["sid"][-8:]), extra))
    resume = _queue_resume(load_config(), t["sid"], t["title"])  # F15: dormant queue
    if resume:
        reply += "\n" + resume
    return reply


def handle_free_text(cfg, text):
    """Free text outside waits: if there is a pending headless ❓ it
    answers it; otherwise it goes to the active conversation
    (headless)."""
    import threading as _th
    # 1) answer to a headless agent question (F15: prefers the current
    # task's; falls back to the only pending one) — F16: file-based
    act = active_bot_session()
    pend = _questions().first_pending((act or {}).get("sid"))
    if pend is not None:
        resp = text.strip()
        qs = pend.get("questions") or []
        # number -> option label/value (from 1: a "0" is not an option
        # — with a negative index it selected the LAST one outright)
        if resp.isdigit() and qs and int(resp) >= 1:
            try:
                ops = (qs[0].get("options") or [])
                op = ops[int(resp) - 1]
                resp = op.get("value") or op.get("label") or resp
            except (IndexError, ValueError):
                pass
        _questions().answer(pend.get("request_id"), resp)
        return "✅ Answered: %s" % esc(resp[:200])
    if not act:
        return ("💬 There is no active conversation yet — send /task and "
                "pick one with /task n.\nAfter that, all text you send "
                "runs there (headless, locked screen OK).")
    sid = act.get("sid")
    if sid and sid in running_turns():
        # F17: the task is busy (Mac or its own turn) → enqueue INLINE,
        # spawning NOTHING: a clear notice per message and a dormant
        # queue. Before, the worker spawned a ~500MB runtime to discover
        # the same thing (and the relay did it in a loop).
        if not _queue_put(sid, act, text, cfg=cfg):
            return ("⚠️ The Mac is busy and I could NOT queue your message "
                    "— retry in a moment.")
        return ("⏳ The Mac is busy with «%s» — your message stayed in the "
                "queue (%d waiting). It runs when the run finishes "
                "(or /stop to clear it; /nuclear in an emergency)."
                % (esc(act.get("title", "?")), len(_queue_peek(sid))))
    if _spawn_worker(cfg, act, text) is None:
        return None  # the error already went out to the chat
    return "▶️ «%s» — working (headless)…" % esc(act.get("title", "?"))


def _cmd_retry(cfg, rest):
    """/retry — relaunches the last prompt as-is (failure, crash or
    timeout). If there is an active task other than the prompt's, ask...
    no: it runs on the prompt's ORIGINAL task (where it has context)."""
    act, text = _load_last_prompt()
    if not text:
        return "🤔 I have no previous prompt to retry."
    if _workers().get((act or {}).get("sid") or "nueva") is not None:
        return "⏳ A turn is already running in «%s» — /stop first if you want."
    if _spawn_worker(cfg, act, text) is None:
        return None
    return "🔁 Retrying «%s»:\n%s" % (esc((act or {}).get("title", "?")),
                                      esc(preview(text, 200)))


def _cmd_stop(cfg, rest):
    """/stop ONLY handles runs started from the phone: whatever runs on
    the Mac is stopped with the app's Stop button, it is not even listed
    here. /stop go = HARD stop: immediate kill -9 of all the bot's
    runtimes (without waiting for the cooperative session/stop)."""
    r = (rest or "").strip().lower()
    if r in ("go", "hard", "9"):
        items = _workers().items()
        if not items:
            return "🤔 There are no bot runs to stop."
        for sid, w in items:
            bot_stop(sid, w.get("titulo"), grace=0.0)
        return "💀 Hard stop: kill -9 of %d runtime(s) — the report " \
               "comes from the orphan check." % len(items)
    head, _rest = args.head_and_rest(rest)
    sid = args.sid_of(head)
    if sid:
        return bot_stop(sid)
    if head.isdigit():
        items = _workers().items()
        k = int(head)
        if 1 <= k <= len(items):
            sid, w = items[k - 1]
            return bot_stop(sid, w.get("titulo"))
        return args.out_of_list_error("stop", len(items))
    return stop_list_report()


def _cmd_task(cfg, rest):
    if not rest:
        return task_list_report("projects")
    if rest.startswith("@ses"):
        return _task_select_sid(rest[1:])
    if rest.isdigit():
        return task_select(int(rest), "projects")
    if rest.split()[0] == "new":
        tag = rest.split(" ", 1)[1].strip() if " " in rest else ""
        ws = _resolve_ws(tag)
        if not ws:
            return ("⚠️ I don't know the project «%s» — try without a name "
                    "(uses the active one) or the folder name." % esc(tag))
        set_active_bot_session({"sid": None, "ws": ws,
                                "title": "new · " + os.path.basename(ws)})
        return ("🆕 New conversation in %s — send the first message and it "
                "starts (headless)." % esc(os.path.basename(ws)))
    return ("⚠️ /task list · /task &lt;n&gt; picks · /task new [project] · "
            "/all_tasks shows all")


def _cmd_all_tasks(cfg, rest):
    if rest.isdigit():
        return task_select(int(rest), "all")
    return task_list_report("all")


def _cmd_queue(cfg, rest):
    """/queue — the CONTENT of the queues (F22): each queued item with
    ⏫ (launch now) and 🗑 (discard) buttons."""
    sids = _queue_sids_with_items()
    if not sids:
        return "📭 No queued messages."
    lines = ["📬 <b>Prompt queue</b>"]
    kb = []
    for sid in sids:
        act_sid = (active_bot_session() or {}).get("sid")
        stamp = " 🎯" if sid == act_sid else ""
        title = session_title_from_db(sid) or sid[-8:]
        lines.append("«%s»%s:" % (esc(title), stamp))
        for i, item in enumerate(_queue_peek(sid), 1):
            lines.append("  %d. %s" % (i, esc(preview(item.get("text", ""), 80))))
            kb.append([
                {"text": "⏫ %d now" % i, "callback_data": "qgo:%s:%d" % (sid, i - 1)},
                {"text": "🗑", "callback_data": "qdel:%s:%d" % (sid, i - 1)}])
    return "\n".join(lines), {"inline_keyboard": kb}


def _queue_mutate(sid, fn):
    """F22: ATOMIC read-modify-write of ONE queue of the dict (does not
    clobber the other sessions). fn(lst) -> new lst (or None = no
    changes). True if it applied (audit 20/10: the swallowed OSError
    made /qgo say «Lanzado» without having removed the item → double
    execution when draining)."""

    def apply(q):
        q = q if isinstance(q, dict) else {}
        new = fn(list(q.get(sid) or []))
        if new is None:
            return None
        if new:
            q[sid] = new
        else:
            q.pop(sid, None)
        return q
    try:
        _store(config.QUEUE_FILE).update_cross_process(apply, {})
        return True
    except OSError as e:
        from zcode_bot.utils.logging import pwlog
        pwlog("queue_mutate FALLO: %s" % e)
        return False


def _cmd_qgo(cfg, rest):
    """/qgo <sid>:<idx> — launches that queued item NOW (removes it from the queue)."""
    parsed = args.parse_sid_idx(rest or "")
    if parsed is None:
        return "⚠️ /qgo sid:idx"
    sid, idx = parsed
    lst = _queue_peek(sid)
    if not (0 <= idx < len(lst)):
        return "🤔 That queued item is no longer there (did you launch it twice?)."
    item = lst[idx]
    if _workers().get(sid) is not None or (sid in running_turns()):
        return "⏳ «%s» is still busy — the running turn drains it at the end." \
            % esc(sid[-8:])
    taken = [[]]

    def take(l):
        taken[0] = l[idx:idx + 1]
        return l[:idx] + l[idx + 1:]
    if not _queue_mutate(sid, take):
        # without removing the item, launching would DUPLICATE it when
        # the queue drains
        return ("⚠️ The queue is stuck — I launched nothing (retry in "
                "a moment).")
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        _queue_put(sid, item["act"], item["text"])  # don't lose it
        return None
    return "⏫ Launched: %s" % esc(preview(item.get("text", ""), 100))


def _cmd_qdel(cfg, rest):
    """/qdel <sid>:<idx> — discards that queued item."""
    parsed = args.parse_sid_idx(rest or "")
    if parsed is None:
        return "⚠️ /qdel sid:idx"
    sid, idx = parsed
    item = [[]]

    def drop(l):
        if not (0 <= idx < len(l)):
            return None
        item[0] = l[idx]
        return l[:idx] + l[idx + 1:]
    if not _queue_mutate(sid, drop):
        return "⚠️ The queue is stuck — nothing discarded, retry."
    if not item[0]:
        return "🤔 That queued item is no longer there."
    return "🗑 Discarded: %s" % esc(preview(item[0].get("text", ""), 100))


def _cmd_run(cfg, rest):
    import threading as _th
    if not rest:
        return "⚠️ /run [project] &lt;prompt&gt; — creates a new conversation and sends the prompt."
    parts = rest.split(" ", 1)
    if len(parts) == 2 and _resolve_ws(parts[0]) and " " in rest:
        ws, prompt = _resolve_ws(parts[0]), parts[1].strip()
    else:
        ws, prompt = active_workspace_path(), rest
    if not prompt:
        return "⚠️ Missing the prompt."
    act = {"sid": None, "ws": ws, "title": "run · " + os.path.basename(ws or "")}
    _spawn_worker(cfg, act, prompt)
    return None
