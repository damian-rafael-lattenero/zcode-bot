"""commands.handlers_info — report and menu handlers (F9).

Moved VERBATIM from commands.dispatch (debt declared in ADR-012, F9
close-out): register_bot_menu, _cmd_quota, _cmd_usage, _cmd_ctx,
_cmd_compact, _cmd_all_compact, _cmd_status, _cmd_last, _cmd_cot and
_cmd_help. These are the READ commands: all the heavy lifting lives in
reporting, here we only adapt the reply to the chat.
"""
import os
import re

from zcode_bot import config
from zcode_bot import proto as zc_proto
from zcode_bot import reasoning
from zcode_bot.core import active_bot_session
from zcode_bot.reporting import (build_trace, _send_long_reply, quota_report,
                                 usage_report, context_report,
                                 compact_list_report, _compact_args,
                                 status_report, _tasks_scope, _ctx_tasks)
from zcode_bot.messages import (_fetch_messages, _extract_assistant_text,
                                _steps_of_messages, colorize_diff)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview, equal_parts
from zcode_bot.utils.logging import log


def register_bot_menu(cfg):
    """Telegram command menu (the '/' button): re-registered at poller
    startup to keep it up to date. R6: the list is DERIVED from the
    dispatch specs (a single place declares command+description; the
    visible ones go in table order)."""
    from zcode_bot.commands.dispatch import SPECS  # late import: dispatch imports us
    cmds = [(s.names[0], s.menu_desc) for s in SPECS if s.menu_desc]
    try:
        params = {"commands": [{"command": c, "description": d} for c, d in cmds]}
        if cfg.get("chat_id"):
            # D1: menu visible ONLY in the owner's chat (official scope)
            params["scope"] = {"type": "chat", "chat_id": cfg["chat_id"]}
        uhttp.tg_api(cfg, "setMyCommands", params)
        return True
    except Exception as exc:  # noqa: BLE001
        log("errors", "setMyCommands: %s" % exc)
        return False


def _cmd_quota(cfg, rest):
    return quota_report()


def _cmd_usage(cfg, rest):
    return usage_report()


def _cmd_ctx(cfg, rest):
    return context_report()


def _cmd_compact(cfg, rest):
    if not rest:
        return compact_list_report("projects")
    return _compact_args(rest, "projects")


def _cmd_all_compact(cfg, rest):
    if not rest:
        return compact_list_report("all")
    return _compact_args(rest, "all")


def _cmd_status(cfg, rest):
    # F38: slow commands in flight — the snapshot lives in dispatch, but
    # dispatch imports THIS module (REG table): late import, no cycle
    from zcode_bot.commands import dispatch as zc_dispatch
    return status_report(inflight=zc_dispatch.inflight_snapshot())


# R8: /diff lives in handlers_diff (verbatim move); re-exported
# because dispatch and the tests import it from here.
from zcode_bot.commands.handlers_diff import _cmd_diff  # noqa: E402,F401


# F56 — the CoT reader window: ONE message per chat where the taps show
# box content. The round's live message is NEVER the tap target anymore:
# the F43c in-place edit ("show it where the little box is") collided
# with F46's per-round messages — a box being born re-edited the very
# message you were READING (Damian, 22/10: "I tap 1.3, 1.4 is generated
# and 1.3 closes on me"). Poller memory: a restart just opens a fresh
# reader on the next tap; the old one stays in the chat, inoffensive.
_READER = {"ph": None}


def _reader_show(cfg, text, kb):
    """Show box content in the reader window: edit it when it exists,
    send it when it doesn't (📖 marks it apart from the live board).
    Returns the reader's message id, None when both fail."""
    kb = {"inline_keyboard":
          ((kb or {}).get("inline_keyboard") or [])
          + [[{"text": "✕", "callback_data": "cc"}]]}
    ph = _READER["ph"]
    if ph and uhttp._live_edit(cfg, ph, "📖 " + text, markup=kb):
        return ph
    ph = reasoning.ph_of(uhttp.send(cfg, "📖 " + text,
                                    loud=False, markup=kb))
    if ph:
        _READER["ph"] = ph
    return ph


def _cmd_cotx(cfg, rest):
    """✕ of the reader window (cc button): deletes it. Hidden command."""
    ph = _READER["ph"]
    _READER["ph"] = None
    if ph:
        uhttp._delete(cfg, ph)
    return None


def _split_tap(rest):
    """F59 — the ct:/cr: pseudo-messages carry the TAPPED message id as a
    self-describing «ph=N» token (updates.normalize_update). → (args
    without the token, ph or None). It can never collide with /cotb's
    positional part argument."""
    toks = (rest or "").split()
    ph = None
    for t in toks:
        if t.startswith("ph="):
            try:
                ph = int(t[3:])
            except ValueError:
                ph = None
    return [t for t in toks if not t.startswith("ph=")], ph


def _cmd_cotb(cfg, rest):
    """/cotb <sid> <t0> <n> [part] [ph=N] — triggered by the «CoT R.C»
    buttons (ct:sid:t0:n[:p]). Reads the cache: instant, no runtime.
    F59: a tap on a box's OWN frozen message (the ph the tap carries is
    THAT box's ph and the cache is per-box) expands it IN PLACE — the
    content lands inside its own message, where the thumb is, with a
    ⤴️ to fold it back; nothing is born at the bottom of the chat.
    Every other tap surface (the reader itself, the legacy round
    messages, a throttled edit) keeps F56: the READER window (one per
    chat, 📖) with F49's keyboard — parts on top, the boxes of ITS
    reasoning block below, ✕ to close."""
    a, ph_tap = _split_tap(rest)
    if len(a) < 3:
        return "🤔 Usage: tap a «CoT n» button of a turn's reasoning."
    try:
        sid, t0, n = a[0], int(a[1]), int(a[2])
        r = reasoning.show(sid, t0, n, int(a[3]) if len(a) > 3 else 1)
    except ValueError:
        return "🤔 Invalid button — send /cot for the full document."
    try:
        import json as _json
        with open(reasoning.cache_path(sid, t0)) as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return r
    phs = d.get("phs") or []
    # n>=1 (a manual /cotb with 0: the negative index pointed at the
    # last reasoning block — audit 20/10)
    ph_n = phs[n - 1] if n >= 1 and len(phs) >= n and phs[n - 1] else None
    text, kb = r if isinstance(r, tuple) else (r, None)
    if ph_tap and ph_n == ph_tap and d.get("perbox"):
        # F59 — the box's own message: the expansion happens HERE. The
        # parts row (if the box has one) stays on top, ⤴️ below it.
        kb2 = {"inline_keyboard": (kb or {}).get(
            "inline_keyboard", []) + reasoning.collapse_buttons(sid, t0, n)}
        if uhttp._live_edit(cfg, ph_tap, text, markup=kb2):
            reasoning.kb_log("cot expand n=%d" % n, ph_tap, kb2)
            return None  # nothing new below: the chat timeline is intact
        # a throttled/failed edit falls through to the reader below
    labels = d.get("labels") or d.get("rotulos") or []  # P6: legacy caches  # noqa: spanish — legacy cache keys, permanent bilingual read
    if labels:
        # F49: the reader's keyboard carries the buttons of the boxes
        # that LIVE in the same message the box belongs to (its
        # reasoning block, or the late ones from the closing); the full
        # index only for the old single-message caches. Before, the
        # viewer stuffed ALL buttons into any message (Damian: "I tapped
        # 1.1 and buttons from 2.1 appeared"). With parts: parts on top
        # and boxes at the bottom.
        if ph_n:
            idxs = [i + 1 for i, p in enumerate(phs) if p == ph_n]
            rows = reasoning.index_buttons(sid, t0, labels, idxs)
        else:
            rows = reasoning.box_buttons(sid, t0, labels)
        kb = {"inline_keyboard": (kb or {}).get("inline_keyboard", [])
              + rows}
    rph = _reader_show(cfg, text, kb)
    if rph:
        reasoning.kb_log("cotb n=%d" % n, rph, kb)
        return None  # the reader took it: the round's message untouched
    return r


def _cmd_cotr(cfg, rest):
    """/cotr <sid> <t0> <n> [ph=N] — the ⤴️ of an F59 expansion: the
    box's own message folds back to its frozen line and ITS single
    button (cr: callback). Silent by design: the edit IS the answer;
    when it cannot run (purged cache, ph mismatch, throttled) the
    button simply stays where it is."""
    a, ph_tap = _split_tap(rest)
    if len(a) < 3 or not ph_tap:
        return "🤔 Usage: tap ⤴️ on an expanded box."
    try:
        sid, t0, n = a[0], int(a[1]), int(a[2])
    except ValueError:
        return "🤔 Invalid button."
    try:
        import json as _json
        with open(reasoning.cache_path(sid, t0)) as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return None  # nothing to restore it to: it stays expanded
    boxes = d.get("boxes") or []
    labels = d.get("labels") or []
    phs = d.get("phs") or []
    if not (d.get("perbox") and 1 <= n <= len(boxes)
            and len(labels) >= n and phs[n - 1] == ph_tap):
        return None  # not this message's box: touch nothing
    label = labels[n - 1]
    kb = {"inline_keyboard": reasoning.box_buttons(sid, t0, [label],
                                                   start=n)}
    if uhttp._live_edit(cfg, ph_tap,
                        reasoning.box_done_text(label, len(boxes[n - 1])),
                        markup=kb):
        reasoning.kb_log("cot collapse n=%d" % n, ph_tap, kb)
    return None


def _cmd_agb(cfg, rest):
    """/agb <sid> <t0> <agent> <box> [part] [ph=N] — the «Agent N.C»
    buttons (F63): the box of a subagent's progress, from the agent's
    cache. Same contract as /cotb: a tap on the box's OWN message (the
    cache is perbox) expands it IN PLACE (F59) with a ⤴️ that folds it
    back (/agr); every other surface falls to the reader."""
    from zcode_bot import agent_live
    a, ph_tap = _split_tap(rest)
    if len(a) < 4:
        return "🤔 Usage: tap an «Agent N.C» button of a running agent."
    try:
        sid, t0, n, c = a[0], int(a[1]), int(a[2]), int(a[3])
        r = reasoning.show(sid, t0, c, int(a[4]) if len(a) > 4 else 1,
                           cache_p=agent_live.agent_cache_path(sid, t0, n))
    except ValueError:
        return "🤔 Invalid button — it may belong to an older turn."
    try:
        import json as _json
        with open(agent_live.agent_cache_path(sid, t0, n)) as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return r
    phs = d.get("phs") or []
    ph_n = phs[c - 1] if c >= 1 and len(phs) >= c and phs[c - 1] else None
    text, kb = r if isinstance(r, tuple) else (r, None)
    if ph_tap and ph_n == ph_tap and d.get("perbox"):
        rows = agent_live._ag_rows((kb or {}).get("inline_keyboard", []), n)
        # the ⤴️ row goes through the same rewrite (cr: → agr: gains the
        # agent segment: agr:sid:t0:agent:box)
        rows += agent_live._ag_rows(
            reasoning.collapse_buttons(sid, t0, c), n)
        kb2 = {"inline_keyboard": rows}
        if uhttp._live_edit(cfg, ph_tap, text, markup=kb2):
            reasoning.kb_log("agent expand %d.%d" % (n, c), ph_tap, kb2)
            return None
    if ph_n:  # the reader's keyboard: that box's own button to re-tap
        kb = {"inline_keyboard": reasoning.agent_buttons(
            sid, t0, n, d.get("labels") or [], start=c)}
    rph = _reader_show(cfg, text, kb)
    if rph:
        reasoning.kb_log("agb %d.%d" % (n, c), rph, kb)
        return None
    return r


def _cmd_subagents(cfg, rest):
    """/subagents — F64 (Damian, 25/10): the agents of the active task's
    current turn, one button each with its descriptive title; pick one
    → its run shown like a task's (status line + «Agent N.C» boxes).
    /subagents n opens agent n directly."""
    from zcode_bot import agent_live
    act = active_bot_session()
    if not act or not act.get("sid"):
        return "⚠️ Pick a conversation first (/task n)."
    sid, title = act["sid"], act.get("title") or act["sid"][-8:]
    r = (rest or "").strip()
    if r.isdigit():
        idx = agent_live.read_index(sid)
        return agent_live.subagent_show(cfg, sid, int(idx.get("t0") or 0),
                                        int(r))
    text, kb = agent_live.subagents_list(sid, title)
    return (text, kb) if kb else text


def _cmd_sa(cfg, rest):
    """/sa <sid> <t0> <n> — the /subagents pick button (sa: callback)."""
    from zcode_bot import agent_live
    a = (rest or "").split()
    if len(a) < 3:
        return "🤔 Usage: tap an agent in /subagents."
    try:
        return agent_live.subagent_show(cfg, a[0], int(a[1]), int(a[2]))
    except ValueError:
        return "🤔 Invalid button."


def _cmd_agr(cfg, rest):
    """/agr <sid> <t0> <agent> <box> [ph=N] — the ⤴️ of an F63 agent
    expansion: folds the box's message back to its frozen line + ITS
    button. Silent by design (the edit IS the answer)."""
    from zcode_bot import agent_live
    a, ph_tap = _split_tap(rest)
    if len(a) < 4 or not ph_tap:
        return "🤔 Usage: tap ⤴️ on an expanded agent box."
    try:
        sid, t0, n, c = a[0], int(a[1]), int(a[2]), int(a[3])
    except ValueError:
        return "🤔 Invalid button."
    try:
        import json as _json
        with open(agent_live.agent_cache_path(sid, t0, n)) as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return None  # nothing to restore it to: it stays expanded
    boxes = d.get("boxes") or []
    labels = d.get("labels") or []
    phs = d.get("phs") or []
    if not (d.get("perbox") and 1 <= c <= len(boxes)
            and len(labels) >= c and phs[c - 1] == ph_tap):
        return None
    label = labels[c - 1]
    kb = {"inline_keyboard": reasoning.agent_buttons(
        sid, t0, n, [label], start=c)}
    if uhttp._live_edit(cfg, ph_tap,
                        "🤖 <b>%s</b> · %s" % (esc(label),
                                               reasoning.fmt_tok(len(boxes[c - 1]))),
                        markup=kb):
        reasoning.kb_log("agent collapse %d.%d" % (n, c), ph_tap, kb)
    return None


def _cmd_last(cfg, rest):
    try:
        with open(os.path.join(config.STATE_DIR, config.LAST_REPLY_FILE),
                  errors="replace") as f:
            text = f.read()
    except OSError:
        return "🤷 No previous reply saved."
    if not text.strip():
        return "🤷 The last reply was empty."
    _send_long_reply(cfg, "full reply", 0, text, max_parts=10)
    return None


def _cmd_cot(cfg, rest):
    """/cot [n|@sid] — step by step + full reasoning of a task as a
    scrollable document. No args: the active one. Also works for tasks
    run in the Mac app (reading history DOES cross runtimes — what does
    not cross is stopping)."""
    import datetime
    tasks = {t["sid"]: t for t in _ctx_tasks(30)}
    target = None
    r = (rest or "").strip()
    if r.startswith("@ses"):
        target = tasks.get(r.split()[0][1:])
        if target is None:
            return "⚠️ I couldn't find that conversation — send /task."
    elif r.isdigit():
        lst = _tasks_scope("all", 20)
        k = int(r)
        if not 1 <= k <= len(lst):
            return "⚠️ Number out of the list (1 to %d) — /all_tasks." % len(lst)
        target = lst[k - 1]
    else:
        act = active_bot_session()
        if act and act.get("sid"):
            target = tasks.get(act["sid"])
        if target is None:
            return "⚠️ Pick a conversation first (/task n) or pass its number."
    try:
        c = zc_proto.make(cwd=target["ws"])
    except OSError as e:
        return "❌ " + esc(str(e)[:100])
    try:
        rr = c.request("session/resume", {"sessionId": target["sid"]}, timeout=60)
        if e := zc_proto.proto_err(rr, "resume"):
            return e
        msgs = _fetch_messages(c, target["sid"], limit=150) or []
        trace = build_trace(target["title"] or target["sid"][-8:],
                            target["sid"], 0.0, msgs)
        try:
            with open(os.path.join(config.STATE_DIR, config.LAST_TRACE_FILE), "w") as f:
                f.write(trace)
        except OSError:
            pass
        fname = "cot-%s-%s.txt" % (
            re.sub(r"[^A-Za-z0-9._-]+", "-",
                   (target["title"] or "ses")[:24]).strip("-"),
            datetime.datetime.now().strftime("%H%M"))
        ok = uhttp.send_document(cfg, fname, trace,
                           "🧠 Trace of «%s»: %d steps · last %d messages"
                           % (target["title"] or target["sid"][-8:],
                              len(_steps_of_messages(msgs)), len(msgs)))
        return ("📄 Document sent." if ok else
                "⚠️ I couldn't send the document — retry /cot.")
    finally:
        c.close()


def _cmd_help(cfg, rest):
    return ("🤖 <b>Unified ZCode bot</b> (headless — works with the Mac locked)\n"
            "<b>Conversations</b>\n"
            "/task — with Project · /task n picks · /task new [project]\n"
            "/all_tasks — all of them, loose Tasks-group ones included\n"
            "free text — runs in the active conversation\n"
            "/run [project] prompt — new conversation + prompt\n"
            "/model — model of the active one · /thinking — max/high/low\n"
            "/mode — Plan mode 🗺 · Full access 🔓 (buttons)\n"
            "/verbosity — how much a turn streams: summary · normal · verbose (buttons)\n"
            "/subagents — the active task's agents (pick one → its run, like a task)\n"
            "/fork — fork the active one · /last — last full reply\n"
            "/cot — step by step + the turn's full CoT (document)\n"
            "<b>Control</b>\n"
            "/quota — plan credits 5h + weekly\n"
            "/usage — today's detail by session\n"
            "/ctx — tasks with context %\n"
            "/ping — liveness check\n"
            "/status — workers (pid), queues, active one's config, health\n"
            "/diff — work in progress (one button per file) · /diff log: progress · /diff commit\n"
            "/queue — content of the queues (⏫ launch / 🗑 discard)\n"
            "/retry — relaunch the last prompt (after a failure or crash)\n"
            "/stop — stops your phone run and CLEARS its message queue\n"
            "/nuclear — ☢️ emergency: stops EVERYTHING (phone + Mac) and restarts\n"
            "/deploy — 🚀 runs the pipeline (tests→backup→reload) with a button\n"
            "/compact — Projects list to compact · /all_compact all\n"
            "(permissions 🔐 and questions ❓ are answered by tapping the buttons, or with text/number)")
