"""reporting — the bot's reports and formatters (F4f).

Everything the owner reads: /quota /usage /ctx /task /compact /stop /status,
a turn's trace and the chunked delivery of long replies. Reads from the
CLI's sqlite/logs + pure formatters; transport BY ATTRIBUTE
(uhttp.send / uhttp.send_document): a single patching seam.
"""
import datetime
import json
import os
import re
from dataclasses import dataclass
import time
import urllib.request

from zcode_bot import config
from zcode_bot import limites as lim
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import cli_logs as zc_logs
from zcode_bot import reports as zc_reports
from zcode_bot import avisos
from zcode_bot import proto as zc_proto
from zcode_bot.core import (active_bot_session, active_workspace_path,
                            session_title_from_db, _cur_sel, _model_snapshot,
                            _restore_model, _wait_cli_event, running_turns,
                            _timeout_by_mb, _compact_timeout, last_used,
                            _workers, _queue_peek, _queue_sids_with_items)
from zcode_bot.live_view import _live_skip_clear, _rss_now_mb
from zcode_bot.messages import (_steps_of_messages, _reasoning_of_messages,
                                _extract_assistant_text)
from zcode_bot.models import MODEL_SEL
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview, fmt_tok, fmt_num, datetime_dt
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log


# Context window per model (tokens). GLM-5.3: official 1M (128K output).
CONTEXT_WINDOWS = {"GLM-5.3": 1000000, "GLM-5.3-Flash": 1000000}

def session_titles():
    """taskId -> title, from the app's session index."""
    import glob as globmod
    titles = {}
    for path in globmod.glob(os.path.join(config.BASE, "v2", "sessions", "*", "ses_*.json")):
        try:
            with open(path) as f:
                meta = json.load(f).get("meta") or {}
            if meta.get("taskId"):
                titles[meta["taskId"]] = meta.get("title") or ""
        except (OSError, ValueError):
            continue
    return titles


def coding_usage(day_offset=0):
    """Real coding plan usage from the CLI's model-io records
    (~/.zcode/cli/rollout/model-io-*.jsonl): per-request usage with real
    values (the text log redacts them). Retention is rolling, so the
    returned window tells from which hour the data reaches.
    Returns (totals, {sessionId: {req, total}}, (from, to)) or (None, None, None).
    """
    import glob as globmod
    target = datetime.date.today() - datetime.timedelta(days=day_offset)
    tot = {"req": 0, "in": 0, "out": 0, "cache": 0, "total": 0}
    per_sess = {}
    window = [None, None]
    for path in globmod.glob(os.path.join(config.BASE, "cli", "rollout", "model-io-*.jsonl")):
        try:
            f = open(path, errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "coding-plan" not in str((d.get("model") or {}).get("providerId") or ""):
                    continue
                ts = d.get("completedAt") or ""
                try:
                    local = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue
                if local.date() != target:
                    continue
                u = (d.get("response") or {}).get("usage") or {}

                def _num(k):
                    try:
                        return int(u.get(k) or 0)
                    except (TypeError, ValueError):
                        return 0
                window[0] = window[0] or local.strftime("%H:%M")
                window[1] = local.strftime("%H:%M")
                tot["req"] += 1
                tot["in"] += _num("inputTokens")
                tot["out"] += _num("outputTokens")
                tot["cache"] += _num("cacheReadTokens")
                tot["total"] += _num("totalTokens")
                sid = str(d.get("sessionId") or "?")
                s = per_sess.setdefault(sid, {"req": 0, "total": 0})
                s["req"] += 1
                s["total"] += _num("totalTokens")
    if not tot["req"]:
        return None, None, None
    return tot, per_sess, tuple(window)


def read_quota_cache():
    """Reads the coding plan quota snapshot that the app's renderer caches
    in its Local Storage (leveldb). It is the same data the app's Usage
    panel shows: limits with currentValue (used), usage (limit),
    percentage and nextResetTime per window. The snapshot is rewritten into
    the leveldb's live .log as plain text; we look for the last occurrence.
    Returns dict {level, windows:[{used,limit,remaining,pct,reset_ms}], mcp} or None.
    """
    ldir = os.path.expanduser("~/Library/Application Support/ZCode/session/Local Storage/leveldb")
    if not os.path.isdir(ldir):
        return None
    files = []
    for name in os.listdir(ldir):
        if name.endswith(".log") or name.endswith(".ldb"):
            p = os.path.join(ldir, name)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
    marker = b'"quota":{"level"'
    best = None  # (highest nextResetTime seen → freshest entry)
    for _, path in sorted(files, reverse=True):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        pos = 0
        while True:
            i = data.find(marker, pos)
            if i < 0:
                break
            pos = i + 1
            # extract the balanced JSON object that starts at "quota":
            start = i + len(b'"quota":')
            depth = 0
            end = None
            in_str = False
            esc = False
            for j in range(start, min(start + 8000, len(data))):
                c = data[j:j + 1]
                if esc:
                    esc = False
                    continue
                if c == b"\\":
                    esc = True
                    continue
                if c == b'"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c == b"{":
                    depth += 1
                elif c == b"}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            if not end:
                continue
            try:
                q = json.loads(data[start:end])
            except ValueError:
                continue
            if not isinstance(q, dict):
                continue
            ts = [l.get("nextResetTime") or 0
                  for l in (q.get("limits") or []) if isinstance(l, dict)]
            if not ts:
                continue  # empty limits: max() of empty used to crash /quota
            freshest = max(ts)
            if best is None or freshest > best[0]:
                best = (freshest, q)
    if not best:
        return None
    q = best[1]
    windows = []
    for lim in q.get("limits") or []:
        if lim.get("type") != "CREDIT_LIMIT":
            continue
        windows.append({
            "hours": lim.get("number"),  # 5 → 5h window, 1 → weekly
            "used": lim.get("currentValue"),
            "limit": lim.get("usage"),
            "remaining": lim.get("remaining"),
            "pct": lim.get("percentage"),
            "reset_ms": lim.get("nextResetTime"),
        })
    windows.sort(key=lambda w: (w["hours"] or 0), reverse=True)  # 5h first, week after
    mcp = None
    agg = q.get("mcpAggregate") or q.get("mcp_aggregate") or {}
    if isinstance(agg, dict) and agg.get("limit"):
        mcp = {"used": agg.get("used") or 0, "limit": agg.get("limit")}
    return {"level": q.get("level"), "windows": windows, "mcp": mcp}


def _quota_api_key():
    """Coding plan API key from bot-provider.json (never log it)."""
    try:
        with open(os.path.join(config.BASE, "hooks", "bot-provider.json")) as f:
            cfg = json.load(f)
        for rule in ((((cfg.get("config") or {}).get("providerConfigRules")
                       or {}).get("providerRules")) or []):
            acc = (rule.get("config") or {}).get("access") or {}
            if acc.get("type") == "zhipu-coding-plan-api-key" and acc.get("apiKey"):
                return acc["apiKey"]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def quota_live():
    """LIVE quota from the panel's endpoint (Bearer with the bot's API key).
    Returns {"level","windows":[{hours,used,limit,pct,reset_ms}]} or None.
    Does not depend on the app having written its cache (the leveldb gets
    compacted and the snapshot disappears — it happened on 11/10)."""
    key = _quota_api_key()
    if not key:
        return None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                "https://api.z.ai/api/monitor/usage/quota/limit",
                headers={"Authorization": "Bearer " + key,
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = (json.loads(r.read().decode()).get("data")) or {}
            out = {"level": data.get("level"), "windows": []}
            for w in data.get("limits") or []:
                used = w.get("currentValue")
                limit = w.get("usage")
                if not isinstance(used, (int, float)) or not limit:
                    continue
                out["windows"].append({
                    "hours": w.get("number"),
                    "used": used, "limit": limit,
                    "pct": w.get("percentage"),
                    "reset_ms": w.get("nextResetTime")})
            return out if out["windows"] else None
        except Exception as exc:  # noqa: BLE001 - flaky network: retry
            log("errors", "quota_live attempt %d: %s" % (attempt + 1, str(exc)[:120]))
            time.sleep(1 + attempt)
    return None


def quota_report():
    """Coding plan: ONLY 5h + weekly credits with resets (like the Usage panel).
    Primary source: the live endpoint; fallback: the app panel's cache.
    Per-session token detail lives in /usage."""
    lines = ["📊 <b>Coding plan</b>"]
    snap = quota_live() or read_quota_cache()
    if snap:
        names = {5: "⏱ 5 hours", 1: "📅 Weekly"}
        for w in snap["windows"]:
            name = names.get(w["hours"], "🔒 Limit")
            pct = f" ({w['pct']}%)" if w.get("pct") is not None else ""
            alert = " ⚠️" if (w.get("pct") or 0) >= 75 else ""
            reset = ""
            if w.get("reset_ms"):
                reset = " · resets " + datetime_dt(w["reset_ms"] / 1000)
            lines.append(f"{name}: {fmt_num(w['used'])}/{fmt_num(w['limit'])}"
                         f" credits{pct}{reset}{alert}")
    else:
        lines.append("(no data — neither the endpoint nor the cache worked)")
    lines.append("Per-session detail: /usage")
    return "\n".join(lines)


def usage_report():
    """Detail of today's usage per session (coding plan)."""
    tot, per_sess, window = coding_usage(0)
    if not tot:
        return "📈 No usage recorded today yet."
    lines = [f"📈 <b>Today's usage by session</b> ({window[0]}–{window[1]})"]
    titles = session_titles()
    rows = sorted(per_sess.items(), key=lambda kv: -kv[1]["total"])
    for sid, s in rows[:8]:
        name = esc(titles.get(sid) or ("…" + sid[-10:]))
        lines.append(f"• {name}: {s['req']} req · {fmt_tok(s['total'])}")
    if len(rows) > 8:
        tail = sum(s["total"] for _, s in rows[8:])
        lines.append(f"• (+{len(rows) - 8} sessions: {fmt_tok(tail)})")
    lines.append(f"<b>Total: {tot['req']} req · {fmt_tok(tot['total'])} tokens</b>")
    return "\n".join(lines)


@dataclass
class Task:
    """Row of the task index (sqlite boundary → reports/lists).
    Compat mapping (get/keys/__getitem__): consumers that still
    index t["sid"] keep working while migrating to attributes."""
    sid: str = ""
    title: str = ""
    ws: str = ""
    model: str = ""
    ctx: int = 0
    when: int = 0  # ms epoch of last activity
    n: int = 0     # visible position (renumbered by _tasks_scope per scope)

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        t = cls()
        for k in ("sid", "title", "ws", "model"):
            setattr(t, k, str(d.get(k) or ""))
        t.ctx = int(d.get("ctx") or 0)
        t.when = int(d.get("when") or 0)
        t.n = int(d.get("n") or 0)
        return t

    def keys(self):
        return ("sid", "title", "ws", "model", "ctx", "when", "n")

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def get(self, k, default=None):
        return getattr(self, k, default)


def _ctx_tasks(limit=10):
    """Tasks with real usage, archived ones excluded, in the app's
    sidebar order (Repository: zcode_bot.tasks_repo). The _ prefix: the
    public name ctx_tasks lives ONLY in tasks_repo (anti-dup gate)."""
    return [Task.from_dict(t)
            for t in zc_repo.ctx_tasks(config.BASE, limit=limit)]


def _compactions(hours=48):
    """{sessionId: epoch} of the last compact.completed per session (scanner
    Strategy: zcode_bot.cli_logs)."""
    lim = time.time() - hours * 3600
    out = {}
    for ts, sid, _ev, _d in zc_logs.iter_events(
            config.BASE, needles=("compact.completed",), since_epoch=lim,
            last_files=2, tail_bytes=0):
        out[sid] = ts
    return out


def context_report():
    """Context per task, numbered like the /compact list. Source: each
    session's latest rollout usage (what the model saw in its most recent
    request — the number the next turn will see). If there was a compact
    NEWER than that request, the session was left compacted with no
    subsequent turn: the only number on disk is the BEFORE one, and it is
    flagged instead of shown as current (16/10: the session/resume probe
    always returned contextUsed=0 — the runtime does not persist the
    projection — and /ctx fell back to the last pre-compact turn paying a
    35s and 3GB resume per query; removed)."""
    tasks = _ctx_tasks(8)
    if not tasks:
        return "🧠 No tasks with recorded usage."
    act = active_workspace_path()
    comp = _compactions()
    lines = ["🧠 <b>Context per task</b> (real state on disk)"]
    for t in tasks:
        window = CONTEXT_WINDOWS.get(t["model"], 1000000)
        cts = comp.get(t["sid"])
        used = last_used(t["sid"])
        if used and cts and used[1] <= cts:
            used = None  # latest on disk predates the compact
        tag = "" if t["ws"] == act else " · 📁" + esc(os.path.basename(t["ws"] or ""))
        title = esc(t["title"] or t["sid"][-8:])
        if used is None and cts and t["when"] / 1000 <= cts:
            hc = datetime.datetime.fromtimestamp(cts).strftime("%H:%M")
            before = f" (before {fmt_tok(t['ctx'])})" if t["ctx"] else ""
            lines.append(f"{t['n']}. 🧹 compacted {hc} — the real size shows"
                         f" on the next turn{before}{tag} — {title}")
            continue
        ctx = int(used[0] * 1000) if used else t["ctx"]
        pct = ctx * 100.0 / window
        emoji = "🔴" if pct >= 80 else ("🟡" if pct >= 50 else "🟢")
        lines.append(f"{t['n']}. {emoji} {fmt_tok(ctx)} ({pct:.0f}%){tag}"
                     f" — {title}")
    lines.append("\n/compact &lt;n&gt; compacts task n (the app only reflects"
                 " the compact when the task is reopened).")
    return "\n".join(lines)


def compact_list_report(scope="projects"):
    """/compact → tappable list (Template Method: zcode_bot.reports)."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    running = running_turns()
    act = active_bot_session()

    def stamp_of(t):
        return ("🔄" if t["sid"] in running
                else ("🎯" if act and t["sid"] == act.get("sid") else "·"))

    def line_of(t, i):
        return "%s %d. %s — %s" % (stamp_of(t), t["n"],
                                   esc(t["title"] or t["sid"][-8:]),
                                   fmt_tok(t["ctx"]))

    def button_of(t, i):
        return {"text": "%s %d · %s (%s)" % (stamp_of(t), t["n"],
                                             (t["title"] or t["sid"][-8:])[:28],
                                             fmt_tok(t["ctx"])),
                "callback_data": "c:%s" % t["sid"]}

    return zc_reports.build_list_report(
        tasks,
        "🧹 <b>Compact — tap one</b> (idle only)"
        + (" · /all_compact for all" if scope == "all" else ""),
        "🧠 No tasks%s to compact — /all_compact shows all."
        % ("" if scope == "all" else " project"),
        line_of, button_of,
        "🔄 cannot (it's running) · 🎯 is the active one.")


def _compact_args(rest, scope):
    """/compact [<n|@sid>] [instructions] → zcode_compact."""
    from zcode_bot.commands import args  # R7 (late: reporting ← handlers)
    head, tail = args.head_and_rest(rest)
    sid = args.sid_of(head)
    n = int(head) if head.isdigit() else None
    if sid is None and n is None:
        tail = rest  # no target given: ALL of it is instructions
    return zcode_compact(tail, n=n, sid=sid, scope=scope)


def zcode_compact(rest="", n=None, sid=None, scope="projects"):
    """Compacts via the ZCode Protocol (headless: no UI, no permissions,
    works with the screen locked). Target: sid (buttons) > n (position in
    the scope's list) > active conversation > most recent task of the
    active project. Idle sessions only. Snapshot/restore of the model
    selection so app sessions are not polluted."""
    import sys as _sys
    import time as _t
    if sid is not None:
        target = {t["sid"]: t for t in _ctx_tasks(30)}.get(sid)
        if target is None:
            return "⚠️ I couldn't find that conversation — send /compact."
    elif n is not None:
        tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
        if n < 1 or n > len(tasks):
            return ("⚠️ Number out of the list (1 to %d) — send /compact."
                    % len(tasks))
        target = tasks[n - 1]
    else:
        act = active_bot_session()
        target = None
        if act and act.get("sid"):
            target = {t["sid"]: t for t in _ctx_tasks(30)}.get(act["sid"])
        if target is None:
            aw = active_workspace_path()
            cands = [t for t in _ctx_tasks(20) if t["ws"] == aw]
            if not cands:
                return ("🤔 No tasks with usage in the active project — "
                        "send /compact to pick one.")
            target = cands[0]
    sid, ws, title = target["sid"], target["ws"], target["title"]
    try:
        c = zc_proto.make(cwd=ws)
    except OSError as e:
        return "❌ I couldn't launch the headless runtime: " + esc(str(e)[:120])
    snap_sel = None
    try:
        r = c.request("session/list", {}, timeout=30)
        if e := zc_proto.proto_err(r, "session/list"):
            return e
        st = {x["sessionId"]: x.get("status")
              for x in (r.get("result") or {}).get("sessions", [])}
        if st.get(sid) == "running":
            return ("⚠️ «%s» is running — stop it first (/stop or the Stop "
                    "button) and retry." % esc(title))
        # F36: «your choice arrived» ack via the component (avisos) — the
        # app already accepted the request (idle session confirmed). Goes
        # before the resume (35s on large sessions) and the compact (minutes).
        avisos.choice_received(
            load_config(), "compacting «%s»" % title,
            "I'll let you know when it's done (state with /ctx)")
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=_timeout_by_mb(sid))
        if e := zc_proto.proto_err(r, "resume"):
            return e
        snap_sel = _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # it was already the bot's
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": MODEL_SEL},
                      timeout=_timeout_by_mb(sid))
        if e := zc_proto.proto_err(r, "setModel"):
            return e
        params = {"sessionId": sid}
        if rest:
            params["instructions"] = rest
        started = _t.time()
        # token-based ceiling (Damian's calibrated rule: 1.2 s/1k tokens);
        # the end event is awaited with the same budget +30
        t_comp = _compact_timeout(sid)
        r = c.request("session/compact", params, timeout=t_comp)
        if e := zc_proto.proto_err(r, "compact"):
            return e
        end_ev = _wait_cli_event(sid, started,
                              ("compact.completed", "compact.failed"),
                              t_comp + 30)
        if end_ev is None:
            return ("🤔 Compaction sent but I didn't see the end event in "
                    "%d s. Check with /ctx." % (t_comp + 30))
        if end_ev.get("event") == "compact.failed":
            err = str((end_ev.get("error") or {}).get("message", ""))[:120]
            return "❌ The compaction of «%s» failed: %s" % (esc(title), esc(err))
        # the compact shrinks the session/events replay: re-enable the
        # live CoT for this session's next turns
        _live_skip_clear(sid)
        dur = (end_ev.get("durationMs") or 0) / 1000.0
        if dur < 2:
            return ("🧹 «%s» compacted (instantly: there was nothing new "
                    "to summarize). Check with /ctx." % esc(title))
        # REAL numbers: the compaction leaves a "compaction" part in the
        # history with exact preCompactTokenCount/postCompactTokenCount
        # (better than estimating from model_usage)
        pre = post = None
        try:
            r = c.request("session/messages", {"sessionId": sid, "limit": 15},
                          timeout=30)
            for m in (r.get("result") or {}).get("messages") or []:
                for p in m.get("parts", []):
                    if p.get("type") == "compaction" and p.get("operationId"):
                        pre = p.get("preCompactTokenCount")
                        # truePost = REAL remaining context; postCompact can
                        # be LARGER than pre (uncut count) and mislead
                        post = (p.get("truePostCompactTokenCount")
                                or p.get("postCompactTokenCount"))
        except Exception:  # noqa: BLE001
            pass
        nums = ""
        if pre and post:
            nums = " Context: %s → %s." % (fmt_tok(pre), fmt_tok(post))
        return ("🧹 Compaction of «%s» done in %.0fs.%s\n"
                "⚠️ If this task is open in the app, reopen it — its window "
                "keeps the previous state until the task is restarted."
                % (esc(title), dur, nums))
    finally:
        c.close()
        if snap_sel:
            _restore_model(sid, snap_sel)


def _tasks_scope(scope, limit):
    """/task → only tasks with a Project (excludes loose ones in the
    default workspace); /all_tasks → all under the Tasks group."""
    tasks = _ctx_tasks(limit)
    if scope != "all":
        tasks = [t for t in tasks if t["ws"] != config.DEFAULT_WS]
    # renumber per the list THAT IS SHOWN: the typed n (/task n) must
    # match the visible number, not the position in the full list
    for i, t in enumerate(tasks, 1):
        t.n = i
    return tasks


def task_list_report(scope="projects"):
    """/task → tappable list (Template Method: zcode_bot.reports)."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    running = running_turns()
    act = active_bot_session()

    def stamp_of(t):
        return ("🔄" if t["sid"] in running
                else ("🎯" if act and t["sid"] == act.get("sid") else "·"))

    def line_of(t, i):
        tag = "" if t["ws"] == active_workspace_path() \
            else " · 📁" + esc(os.path.basename(t["ws"] or ""))
        return "%s %d. %s%s" % (stamp_of(t), t["n"],
                                esc(t["title"] or t["sid"][-8:]), tag)

    def button_of(t, i):
        return {"text": "%s %d · %s" % (stamp_of(t), t["n"],
                                        (t["title"] or t["sid"][-8:])[:34]),
                "callback_data": "t:%s" % t["sid"]}

    header = ("💬 <b>All conversations</b> (loose ones included) — /task only projects"
              if scope == "all"
              else "💬 <b>Conversations by project</b> — tap one (or /task n)")
    return zc_reports.build_list_report(
        tasks, header,
        "🧠 No tasks%s — /all_tasks shows all."
        % ("" if scope == "all" else " project"),
        line_of, button_of,
        "Free text you send goes to the active conversation (🎯).")


def stop_list_report():
    """/stop → runs from the phone + dormant queues (Template Method)."""
    items = _workers().items()
    queued = _queue_sids_with_items()

    def mins_of(par):
        return max(0, int((time.time() - par[1].get("t0", time.time())) / 60))

    def line_of(par, i):
        sid, w = par
        n = len(_queue_peek(sid))
        note = ("\n    ⏳ %d queued (cleared on stop)" % n) if n else ""
        return "%d. «%s» — %d min\n    %s%s" % (
            i, esc(w.get("titulo") or sid[-8:]), mins_of(par),
            esc(preview(w.get("prompt") or "", 70)), note)

    def button_of(par, i):
        sid, w = par
        return {"text": "⏹ %d · %s (%dm)" % (i,
                                              (w.get("titulo") or sid[-8:])[:30],
                                              mins_of(par)),
                "callback_data": "s:%s" % sid}

    # sessions with a queue but NO worker: the run belongs to the Mac (it
    # cannot be stopped from here) — still offer clearing its queue
    extras = [(sid, None) for sid in queued
              if _workers().get(sid) is None]

    def queue_line(par, i):
        sid, _ = par
        act = active_bot_session()
        mark = " 🎯" if act and sid == act.get("sid") else ""
        n = len(_queue_peek(sid))
        return "%d. ⏳ Queue of «%s»%s — %d message%s (Mac run)" % (
            i, esc(sid[-8:]), mark, n, "s" if n != 1 else "")

    def queue_button(par, i):
        return {"text": "🧹 %d · clear queue" % i,
                "callback_data": "s:%s" % par[0]}

    body = zc_reports.build_list_report(
        items, "🛑 <b>Phone runs — tap to stop</b>",
        "😌 Nothing running started from the phone.\n"
        "What runs on the Mac is stopped with the app's Stop button.",
        line_of, button_of) if items else None
    queue_report = zc_reports.build_list_report(
        extras, "📬 <b>Queued messages (Mac busy) — tap to clear</b>",
        None, queue_line, queue_button) if extras else None
    if body and queue_report:
        # (audit 20/10) build_list_report returns (text, markup):
        # the former tuple + str blew up with TypeError and /stop crashed with
        # «⚠️ Error en /stop» exactly when there is a run AND a dormant queue —
        # the emergency scenario where it is needed most
        return (body[0] + "\n\n" + queue_report[0],
                {"inline_keyboard":
                 (body[1] or {}).get("inline_keyboard", [])
                 + (queue_report[1] or {}).get("inline_keyboard", [])})
    return body or queue_report or zc_reports.build_list_report(
        [], "🛑 <b>Phone runs — tap to stop</b>",
        "😌 Nothing running started from the phone.\n"
        "What runs on the Mac is stopped with the app's Stop button.",
        line_of, button_of)


def build_trace(title, sid, dur_min, msgs, live_cot=""):
    """The turn's .txt document: numbered steps + full reasoning +
    answer. Authoritative: the history (msgs); live_cot is the fallback
    in case the history carries no reasoning parts."""
    steps = _steps_of_messages(msgs)
    cot = _reasoning_of_messages(msgs) or live_cot
    answer = _extract_assistant_text(msgs)
    L = ["╔═ TURN: %s" % (title or "?"),
         "║ session: %s" % (sid or "?"),
         "║ %s · %.1f min" % (datetime.datetime.now().strftime(
             "%Y-%m-%d %H:%M"), dur_min),
         "╚" + "═" * 26, ""]
    L.append("════════ STEPS (%d) ════════" % len(steps))
    for i, (name, desc) in enumerate(steps, 1):
        L.append("%3d. %s%s" % (i, name, (" · " + desc) if desc else ""))
    L.append("")
    L.append("════════ FULL REASONING (%d chars) ════════" % len(cot))
    L.append(cot or "(no reasoning recorded)")
    L.append("")
    L.append("════════ REPLY ════════")
    L.append(answer or "(no reply text)")
    return "\n".join(L)


def _turn_trace(cfg, title, sid, dur_min, msgs, live_cot=""):
    """When a turn ends: persists the trace and SENDS IT ONLY if
    trace_documents=true (default OFF since F19 — Damian's request: the
    CoT is already scrolleable in the chat boxes; the file was redundant
    noise. /cot generates it on demand when asked). The
    state/last-turn-trace.txt file is always kept."""
    try:
        trace = build_trace(title, sid, dur_min, msgs, live_cot=live_cot)
    except Exception as exc:  # noqa: BLE001 - never break delivery
        log("errors", "build_trace: %s" % exc)
        return
    try:
        with open(os.path.join(config.STATE_DIR, config.LAST_TRACE_FILE), "w") as f:
            f.write(trace)
    except OSError:
        pass
    if not cfg.get("trace_documents", False):
        return
    fname = "turn-%s-%s.txt" % (
        re.sub(r"[^A-Za-z0-9._-]+", "-", (title or "ses")[:24]).strip("-"),
        datetime.datetime.now().strftime("%H%M"))
    np = len(_steps_of_messages(msgs))
    ncot = len(_reasoning_of_messages(msgs) or live_cot)
    uhttp.send_document(cfg, fname, trace,
                  "🧠 Trace «%s»: %d steps · CoT %s chars · reply in a "
                  "separate message"
                  % (title or "?", np, fmt_tok(ncot)))


def _parts_by_escape(text, cap):
    """Slices whose ESCAPED TEXT fits in cap (deep audit 20/10): esc()
    inflates &<> up to 5x — slices of cap RAW chars with code inside
    inflated to ~5·cap and send() truncated them: the reply arrived
    silently cut short with the «(2/3)» lying."""
    parts, i = [], 0
    while i < len(text):
        chunk = text[i:i + cap]
        inflated = len(esc(chunk))
        while inflated > cap and len(chunk) > 1:
            chunk = chunk[:max(1, int(len(chunk) * cap / inflated))]
            inflated = len(esc(chunk))
        parts.append(chunk)
        i += len(chunk)
    return parts


def _send_long_reply(cfg, title, dur_min, text, max_parts=3):
    """Full reply chunked into ~3900-char messages (max 3 + notice
    with /last for the rest). Saves the full text for /last."""
    parts = _parts_by_escape(text, lim.CH_RESPUESTA - 300)
    try:
        with open(os.path.join(config.STATE_DIR, config.LAST_REPLY_FILE), "w") as f:
            f.write(text)
    except OSError:
        pass
    if len(parts) > max_parts:
        tail = sum(len(p) for p in parts[max_parts:])
        parts = parts[:max_parts]
        note = "\n\n(+%d more characters — send /last)" % tail
    else:
        note = ""
    for i, p in enumerate(parts):
        # 🤖 bot: explicit origin (Mac turns say 🖥️ Mac — F32)
        pre = ("✅ «%s» done in %.0f min · 🤖 bot" % (esc(title), dur_min)
               ) if i == 0 else "···"
        suf = " (%d/%d)" % (i + 1, len(parts)) if len(parts) > 1 else ""
        uhttp.send(cfg, pre + suf + "\n" + esc(p) + (note if i == len(parts) - 1 else ""),
                   loud=False)


def status_report(inflight=None):
    """inflight: [(base, t0, text)] — slow commands IN FLIGHT (F38).
    Passed by handlers_info (a snapshot of dispatch.INFLIGHT) because reporting
    does not import commands: circular."""
    act = active_bot_session()
    lines = ["📊 <b>Status</b>"]
    if act:
        tag = os.path.basename(act.get("ws") or "")
        lines.append("🎯 Active: «%s»%s" % (esc(act.get("title", "")),
                                           " · 📁" + esc(tag) if tag else ""))
        # F22: the active session's settings at a glance
        sel = _cur_sel(act)
        lines.append("🤖 %s · 🧠 %s · 🎛 %s"
                     % (esc(sel.get("modelId", "?")),
                        esc((sel.get("options") or {}).get("reasoningLevel",
                                                           "high")),
                        esc(_mode_label(act.get("mode") or "yolo"))))
    else:
        lines.append("🎯 Active: none (/task n to pick one)")
    workers = _workers().items()
    if workers:
        lines.append("🤖 <b>Bot workers</b>:")
        for sid, w in workers:
            mins = max(0, int((time.time() - (w.get("t0") or time.time())) / 60))
            lines.append("  • «%s» pid %d · %d min · %s"
                         % (esc(w.get("titulo") or sid[-8:]), w.get("pid") or 0,
                            mins, esc(preview(w.get("prompt") or "", 50))))
    for sid in _queue_sids_with_items():
        if _workers().get(sid) is None:  # dormant queues (live ones are shown above)
            n = len(_queue_peek(sid))
            act_sid = (act or {}).get("sid")
            mark = " 🎯" if act and sid == act_sid else ""
            lines.append("📬 Dormant queue «%s»%s — %d message%s (/queue)"
                         % (esc(sid[-8:]), mark, n, "s" if n != 1 else ""))
    running = running_turns()
    if running:
        for sid, ts in running.items():
            if _workers().get(sid) is not None:
                continue  # already listed above as a worker with detail
            title = session_title_from_db(sid) or sid[-8:]
            lines.append("🔄 «%s» since %s (Mac run)"
                         % (esc(title),
                            datetime.datetime.fromtimestamp(ts).strftime("%H:%M")))
    if inflight:
        lines.append("⚡ <b>Commands in flight</b> (thread, result separately):")
        for base, t0, _txt in inflight:
            mins = max(0, int((time.time() - t0) / 60))
            lines.append("  • /%s · %s"
                         % (esc(base), "%d min" % mins if mins else "just now"))
    if (not workers and not running and not _queue_sids_with_items()
            and not inflight):
        lines.append("😌 Nothing running.")
    lines.append("🫀 poller %d · up %s · RSS %dMB"
                 % (os.getpid(), _uptime_txt(), _rss_now_mb()))
    return "\n".join(lines)


def _uptime_txt():
    s = int(time.time() - _POLLER_T0)
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def _mode_label(mode):
    return dict(MODES).get(mode or "yolo", mode or "yolo")


def _resolve_ws(tag):
    """Resolves a project tag ('bot', 'cv', haskell...) to a path."""
    act = active_workspace_path()
    if not tag:
        return act
    tagl = tag.strip().strip("/").lower()
    cands = set([act]) | {t["ws"] for t in _ctx_tasks(20) if t.get("ws")}
    for w in cands:
        if w and os.path.basename(w).lower() == tagl:
            return w
    for w in cands:
        if w and tagl in os.path.basename(w).lower():
            return w
    return None


_POLLER_T0 = time.time()  # process start (for /status)


MODES = (("plan", "🗺 Plan mode"), ("yolo", "🔓 Full access"))

