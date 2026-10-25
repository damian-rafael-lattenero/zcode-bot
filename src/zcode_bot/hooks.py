"""hooks — the entrypoints fired by the ZCode CLI (F4j).

Each on_* is a short-lived process that reads the payload from stdin,
notifies the owner and (for permissions/questions) waits for the reply
over the chat with wait_reply. The cmd_* are the long-running CLI
entrypoints (register, stuck-watch, watch-app). Permission with
anti-loop replay (incident 13/10). Transport BY ATTRIBUTE (uhttp.*).

THE Mac↔celu MODEL (F51, decided by Damian 21/10): ONE shared session,
turns SERIALIZED by the queue — the phone is an extension of the Mac,
not a branch. Fork is MANUAL ONLY (/fork) for deliberate branching;
session merge DOES NOT EXIST and is not wanted (semantic
reconciliation). The bot NEVER stops the app's turn: whoever does not
want to wait goes to the Mac's Stop (or /nuclear in an emergency,
which restarts everything). The mirror ALWAYS shows the Mac's current
task (only_active_task).
"""
import json
import os
import re
import subprocess
import sys
import time

from zcode_bot import config
from zcode_bot import fswatch
from zcode_bot.limits import CH_TURN, PARTES_MAX
from zcode_bot.core import (_hook_sid, _task_current_ok, _worker_owned,
                            _perm_key, _perm_remember, _perm_replay,
                            _workers, _questions, _queue_peek)
from zcode_bot.updates import (current_offset, fetch_updates, save_offset)
from zcode_bot.workers import _queue_resume
from zcode_bot.commands.dispatch import wait_reply
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import (readable_duration, esc, preview,
                                        labeled, task_label)
from zcode_bot.utils.validation import first
from zcode_bot.utils.config import load_config, save_config, configured
from zcode_bot.utils.logging import log, log_payload


def tool_desc(payload):
    name = first(payload, "tool_name", "toolName", "tool") or "tool"
    inp = first(payload, "tool_input", "toolInput", "input") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"input": inp}
    return str(name), inp


def emit(obj):
    """Prints the hook's JSON output (strict schema) and exits."""
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


# moved to config (F34b): ONE definition of the name; what stays here
# is the re-export used by gateway and spectator
from zcode_bot.config import marker_path  # noqa: E402  F34b


def _full_turn(cfg, header, text, max_parts=PARTES_MAX):
    """F32 — Damian's request: the Mac's turn arrives COMPLETE on the
    celu, not as a preview. Chunks at ~3800 chars; only the first part is
    loud (one notification, the rest silent). If the text exceeds
    partes_max, the last part flags the cut — the full text stays in the
    session (/cot)."""
    chunks = [text[i:i + CH_TURN] for i in range(0, len(text), CH_TURN)] or [""]
    cut = len(chunks) > max_parts
    rest = 0
    if cut:
        rest = sum(len(t) for t in chunks[max_parts:])
        chunks = chunks[:max_parts]
    for i, t in enumerate(chunks):
        note = " (%d/%d)" % (i + 1, len(chunks)) if len(chunks) > 1 else ""
        if cut and i == len(chunks) - 1:
            note += ("\n(+%d more characters — they stayed in the session, "
                     "/cot has the trace)" % rest)
        uhttp.send(cfg, (header if i == 0 else "···") + "\n" + esc(t) + note,
                   loud=(i == 0))


def _turn_data(payload, t0=None):
    """Line of relevant turn data. Starts with the ORIGIN: hooks only run
    for app turns (the bot's are reported by the worker in its own
    format) — explicit because the workspace can be named "bot" and
    cause confusion (Damian's request, 19/10)."""
    data = ["🖥️ Mac"]
    steps = payload.get("toolCallCount")
    try:
        steps = int(steps)
    except (TypeError, ValueError):
        steps = None
    if steps is not None:
        data.append("🧰 %d step%s" % (steps, "s" if steps != 1 else ""))
    if t0:
        data.append("⏱ %.0f min" % max(0, (time.time() - t0) / 60))
    # workspace with its parent: a project just named "bot" was read as
    # the message's ORIGIN and came out as "Mac - Bot" (19/10) — with the
    # relative path it is clear it is a folder
    parts = [p for p in str(payload.get("cwd") or "").rstrip("/").split("/")
              if p]
    ws = "/".join(parts[-2:]) if parts else ""
    if ws:
        data.append("📁" + esc(ws))
    return (" · " + " · ".join(data)) if data else ""


def _shim_path():
    """Entrypoint for the subprocesses (stuck-watch, spectator,
    cot-live/cot-buttons): the package's zcode-telegram.py shim, or this
    file as a fallback."""
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")
    return me if os.path.exists(me) else os.path.abspath(__file__)


def on_user_prompt(cfg, payload):
    prompt = str(first(payload, "prompt", "text", "message") or "")
    session = str(first(payload, "session_id", "sessionId") or "ses")
    # F63 (Diff Ask Turnos): this hook is USER-scope, so it fires for EVERY
    # user prompt on this Mac — desktop sessions AND bot/headless turns. Each
    # one appends a turn event (append-only jsonl); the diff-ask plugin's
    # server (one per session) filters by its active workspace and photographs
    # the state BEFORE the agent starts editing. The headless anti-suicide
    # guard is noise in a turn title — peeled off. Non-fatal by design: the
    # panel just doesn't get that turn.
    try:
        ev_dir = os.path.join(os.path.expanduser("~"), ".zcode", "diff-ask")
        os.makedirs(ev_dir, exist_ok=True)
        clean_prompt = prompt.split("\n\n—\n⚠️ (notice from the bot")[0]
        with open(os.path.join(ev_dir, "turn-events.jsonl"), "a") as f:
            f.write(json.dumps({
                "workspace": str(payload.get("cwd") or os.getcwd()),
                "prompt": clean_prompt,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "zcode-hook",
            }) + "\n")
    except OSError:
        pass
    token = f"{time.time_ns()}"
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(marker_path(session), "w") as f:
        f.write(json.dumps({"token": token, "prompt": preview(prompt, 200)}))
    # F15: session with its own worker → no stuck-watch (the worker already
    # has its 30 min timeout + live box) and no ▶️ (double-stream)
    worker = _worker_owned(session)
    if not worker:
        me = _shim_path()
        subprocess.Popen(
            [sys.executable, me, "--stuck-watch", token, session],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # F33: spectator — the Mac's turn streams to the phone (tool
        # boxes + narration from the rollout; the final reply is sent by
        # on_stop/F32). Current task only, no worker (same gate as above)
        # F41: the turn's CoT LIVE — a «pensando…» box whose keyboard
        # grows one button per reasoning block (Damian's design).
        # The cache written at startup makes on_stop skip F40's
        # --cot-buttons (the watcher owns the turn).
        # F44: single-flight — if a HOT watcher for this session already
        # exists (live pid, fresh heartbeat) it takes the turn with the
        # connection it already has; spawning another meant paying the
        # ~45s resume on EVERY prompt.
        if cfg.get("mac_cot_buttons") and _task_current_ok(cfg, session):
            from zcode_bot import reasoning
            if not reasoning.watcher_alive(session):
                try:
                    t0_cot = int(os.path.getmtime(marker_path(session)))
                except OSError:
                    t0_cot = int(time.time())
                subprocess.Popen(
                    [sys.executable, me, "--cot-live", session,
                     task_label(payload), str(t0_cot),
                     str(payload.get("cwd") or os.getcwd())],
                    start_new_session=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        if cfg.get("mac_spectator") and _task_current_ok(cfg, session):
            from zcode_bot import spectator  # lazy: spectator imports this module
            # F62: single-flight — an attach-spawned spectator may already
            # be on this turn; a second one would duplicate every step
            if not spectator.alive(session):
                subprocess.Popen(
                    [sys.executable, me, "--spectator", session,
                     str(payload.get("cwd") or os.getcwd())],
                    start_new_session=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        # F63: the turn's subagents, from the inside — one watcher per
        # turn (Task tool spawns stay blue-and-mute without it)
        if cfg.get("agent_stream") and _task_current_ok(cfg, session):
            from zcode_bot import agent_live  # lazy: keeps the import DAG flat
            if not agent_live.watcher_up(session):
                try:
                    t0_ag = int(os.path.getmtime(marker_path(session)))
                except OSError:
                    t0_ag = int(time.time())
                subprocess.Popen(
                    [sys.executable, me, "--agent-live", session,
                     task_label(payload), str(t0_ag),
                     str(payload.get("cwd") or os.getcwd())],
                    start_new_session=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        if cfg.get("notify_prompt") and _task_current_ok(cfg, session):
            # F32: FULL prompt (before: 200-char preview)
            _full_turn(cfg, labeled(payload, "▶️ <b>Turn started</b>"
                                         + _turn_data(payload)), prompt)


def on_stop(cfg, payload):
    session = str(first(payload, "session_id", "sessionId") or "ses")
    t0 = None  # F32: marker mtime = turn start (duration)
    try:
        t0 = os.path.getmtime(marker_path(session))
    except OSError:
        pass
    try:
        os.remove(marker_path(session))
    except OSError:
        pass
    # F17: the Mac freed the task — if queued messages remain (sent while
    # the task was busy), wake the queue NOW (auto-resume). Only for
    # sessions without a bot worker (the worker's drain owns that
    # relay) and only the current task (F15: the rest sleep until /task).
    try:
        if _queue_peek(session) and _workers().get(session) is None \
                and _task_current_ok(cfg, session):
            res = _queue_resume(load_config(), session)
            if res:
                uhttp.send(cfg, res, loud=False)
    except Exception:  # noqa: BLE001 - the hook never breaks the session
        pass
    if not cfg.get("notify_stop"):
        return
    if _worker_owned(session) or not _task_current_ok(cfg, session):
        return  # F15: the marker still gets cleaned up (side effect ≠ notification)
    # F32: FULL reply (before: a 500-char preview). responseText carries
    # the turn's complete text; responsePreview stays last in case some
    # event only carries the short version
    resp = str(first(payload, "responseText", "response",
                     "last_assistant_message", "stop_response",
                     "responsePreview") or "(no response)")
    # F40: the turn's reasoning with one button per block — the Mac's CoT
    # cannot be watched live (ADR-017) but once the turn closes it is
    # whole in the history. Subprocess: the hook does not wait for the resume.
    # F51a: when the live one did not take over, the fallback pays a full
    # history resume (~2 min on large sessions) and the boxes arrive
    # DELAYED — the ✅ announces it so they are not read as another turn's
    # (incident 21/10 08:18: boxes from the 07:43 turn landing on
    # a freshly started turn).
    from zcode_bot import reasoning  # F41: the live one is the owner
    # F51a: honest gate — suppressed only if the live one CLOSED (finished
    # cache) or is still ALIVE (fresh heartbeat). A cache from a live
    # watcher that died midway no longer silences the turn's close.
    slow = bool(cfg.get("mac_cot_buttons") and t0
                 and not reasoning.cache_closed_or_live(session, int(t0)))
    _full_turn(cfg, labeled(payload, "✅ <b>Turn finished</b>"
                                + _turn_data(payload, t0=t0)
                                + (" · 🧠 reasoning boxes on the way"
                                   " (~2 min)" if slow else "")), resp)
    if slow:
        subprocess.Popen(
            [sys.executable, _shim_path(), "--cot-buttons", session,
             task_label(payload), str(int(t0)),
             str(payload.get("cwd") or os.getcwd())],
            start_new_session=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def on_permission_request(cfg, payload):
    if not configured(cfg):
        return  # no setup: the app's dialog wins, we expect nothing
    name, inp = tool_desc(payload)
    if str(name) == "AskUserQuestion":
        # questions are handled by the ❓ flow (PreToolUse + the integrated
        # bot's buttons): do not duplicate with 🔐 or compete for the getUpdates offset
        return
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        # F15: session with its own worker (its headless permissions are
        # reported by the worker) or inactive task — no message, no wait:
        # the app's dialog wins instantly. Before: the tool call was held
        # up to 240 s for a session nobody was watching, competing for getUpdates.
        return
    is_plan = str(name) in ("ExitPlanMode", "exit_plan_mode")
    key = _perm_key(sid, str(name), inp)
    # FIX approval-loop 13/10: if this EXACT request was already answered
    # recently, resend the same decision without bothering the user again —
    # the CLI sometimes re-fires the hook and the user ended up approving in a loop.
    prev = _perm_replay(key)
    if prev == "allow":
        uhttp.send(cfg, "👍 Already approved a moment ago — resending the decision.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow"}}})
        return
    if prev == "deny":
        uhttp.send(cfg, "🚫 Already denied a moment ago — resending the decision.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "deny",
                                                  "message": "Denied by the user from Telegram"}}})
        return
    if is_plan:
        lines = ["🗺 <b>Plan ready for approval</b>"]
        lines.append("\nTap ✅ to approve it and start the implementation, "
                     "or ✏️ to ask for changes (you type what to change).")
    else:
        lines = [f"🔐 <b>Approval needed</b>: {esc(name)}"]
        if isinstance(inp, dict):
            for k, v in list(inp.items())[:6]:
                lines.append(f"• <b>{esc(str(k))}</b>: {esc(preview(v, 160))}")
        lines.append(f"\nReply <b>yes</b> / <b>no</b> (or tap a button — waiting up to {readable_duration(cfg.get('wait_timeout_s', 240))})")
    kb = [[{"text": "✅ Approve", "callback_data": "yes"},
           {"text": "❌ Reject", "callback_data": "no"}]]
    if is_plan:
        kb.append([{"text": "✏️ Add something to the plan", "callback_data": "planfix"}])
    uhttp.send(cfg, labeled(payload, "\n".join(lines)),
         markup={"inline_keyboard": kb})
    kind, text = wait_reply(cfg, "pending approval")
    if kind == "yes":
        _perm_remember(key, "allow")
        uhttp.send(cfg, "👍 Approved from Telegram — the plan is now executing.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow"}}})
        return
    if kind == "no":
        _perm_remember(key, "deny")
        uhttp.send(cfg, "🚫 Denied from Telegram", loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "deny",
                                                  "message": "Denied by the user from Telegram"}}})
        return
    if kind == "text" and (text or "").strip() == "planfix":
        # ✏️ plan adjustments: ask for the text and deny with that feedback
        uhttp.send(cfg, "✏️ Write what you want changed in the plan (any "
                  "/ command cancels and leaves the plan pending):", loud=False)
        kind2, text2 = wait_reply(cfg, "plan adjustments")
        feed = (text2 or "").strip() if kind2 == "text" else ""
        if not feed or feed.startswith("/"):
            uhttp.send(cfg, "🙅 No changes — it stayed pending in the app.", loud=False)
            return
        _perm_remember(key, "deny")
        uhttp.send(cfg, "✏️ Changes sent to the agent: %s"
             % esc(preview(feed, 200)), loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {
                                         "behavior": "deny",
                                         "message": ("The user asks for changes to the plan: %s"
                                                     " — update it and propose it again."
                                                     % feed)}}})
        return
    if kind is None:
        uhttp.send(cfg, "⏱ No reply over Telegram: the dialog stayed in the app (if you already answered there, ignore this).")
    # no decision: the app's dialog wins the race


def on_ask_user_question(cfg, payload):
    _, inp = tool_desc(payload)
    questions = []
    if isinstance(inp, dict):
        q = inp.get("questions")
        if isinstance(q, list):
            questions = q
        elif inp.get("question"):
            questions = [inp]
    if not questions:
        questions = [{"question": preview(inp, 300)}]
    if not configured(cfg):
        return  # the question is shown normally in the app
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: the dialog stays on the Mac; the worker reports its own
    # FIX 14/10 (repeated questionnaires): the CLI re-fires the hook and
    # the user ended up picking the option OVER AND OVER. Same fingerprint
    # answered <10 min ago → same answer, nothing re-shown.
    key = _perm_key(sid, "AskUserQuestion", inp)
    prev = _perm_replay(key)
    if prev:
        uhttp.send(cfg, "📨 Already answered a moment ago — resending: %s"
             % esc(preview(prev, 120)), loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                     "permissionDecision": "deny",
                                     "permissionDecisionReason":
                                     "The user answered from Telegram: %s"
                                     % prev}})
        return
    lines = ["❓ <b>The agent asks</b>"]
    for i, q in enumerate(questions, 1):
        lines.append(f"\n<b>{i}. {esc(preview(q.get('question', ''), 200))}</b>")
        for j, opt in enumerate(q.get("options", [])[:4], 1):
            label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""
            lines.append(f"   {j}. {esc(preview(label, 80))}" + (f" — {esc(preview(desc, 100))}" if desc else ""))
    if cfg.get("question_mode") != "phone":
        # mac: notify the phone and return at once — the dialog appears on the Mac instantly.
        # From the phone, the reply path is the integrated bot (it enters as a prompt).
        lines.append("\n💻 The dialog is already open on the Mac: answer there."
                     "\n📱 Not at the Mac? The question arrives with buttons at @glm_lattenero_bot — answer there.")
        uhttp.send(cfg, labeled(payload, "\n".join(lines)))
        return
    lines.append(f"\nReply with the number (or tap a button; waiting {readable_duration(int(cfg.get('question_wait_timeout_s')))}, then it also appears on the Mac)")
    kb = []
    q0 = questions[0] if questions else {}
    for j, opt in enumerate((q0.get("options") or [])[:4], 1):
        label = opt.get("label", "?") if isinstance(opt, dict) else str(opt)
        kb.append([{"text": ("%d. %s" % (j, label))[:36], "callback_data": "ans:%d" % j}])
    uhttp.send(cfg, labeled(payload, "\n".join(lines)),
         markup={"inline_keyboard": kb} if kb else None)
    qcfg = dict(cfg, wait_timeout_s=int(cfg.get("question_wait_timeout_s")))
    kind, text = wait_reply(qcfg, "pending question")
    if kind is None:
        uhttp.send(cfg, "⏱ No reply: the question stayed in the app.")
        return  # no decision: the question is shown in the app
    m = re.match(r"^([0-9]+)\b", text or "")
    if m:
        answer = f"option {m.group(1)}"
    elif kind in ("yes", "no"):
        answer = "yes" if kind == "yes" else "no"
    else:
        answer = text or ""
    uhttp.send(cfg, f"📨 Answer sent: {esc(preview(answer, 200))}", loud=False)
    _perm_remember(key, answer)  # FIX 14/10: replay if the CLI re-fires
    # we block the tool; the reason reaches the agent as context
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                 "permissionDecision": "deny",
                                 "permissionDecisionReason": f"The user answered from Telegram: {answer}"}})


def on_tool_failure(cfg, payload):
    if not cfg.get("notify_failures"):
        return
    name, _ = tool_desc(payload)
    err = str(first(payload, "error", "tool_error", "stderr") or "")
    for pat in cfg.get("suppress_error_patterns") or []:
        try:
            if re.search(pat, err, re.IGNORECASE):
                return  # self-recoverable error: the agent is already fixing it
        except re.error:
            continue
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: another task's failure, or already reported by its worker
    uhttp.send(cfg, labeled(payload, f"❌ <b>Failed</b> {esc(name)}: {esc(preview(err, 220))}"))


def on_session_start(cfg, payload):
    if not cfg.get("notify_session"):
        return
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: every headless turn does a resume — that is not news
    source = str(first(payload, "source", "reason", "trigger") or "startup")
    emoji = {"startup": "🟢", "resume": "🔄", "clear": "🧹", "compact": "📦"}.get(source, "🟢")
    uhttp.send(cfg, labeled(payload, f"{emoji} <b>Session</b> ({esc(source)})"))


def run_hook():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"_raw": raw[:2000]}
    cfg = load_config()
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or
                os.environ.get("ZCODE_HOOK_EVENT", "unknown"))
    log_payload(event, payload)
    if event == "PreToolUse":
        name, _ = tool_desc(payload)
        if str(name) == "AskUserQuestion":
            on_ask_user_question(cfg, payload)
        return
    handler = {
        "UserPromptSubmit": on_user_prompt,
        "Stop": on_stop,
        "PermissionRequest": on_permission_request,
        "PostToolUseFailure": on_tool_failure,
        "SessionStart": on_session_start,
    }.get(event)
    if handler:
        handler(cfg, payload)


def cmd_register():
    cfg = load_config()
    if not cfg.get("token"):
        print("Missing token: zcode-telegram.py set-token <TOKEN>")
        sys.exit(1)
    print("Send the bot a message from Telegram (waiting up to 120 s)…")
    deadline = time.time() + 120
    offset = current_offset()
    while True:
        rest = deadline - time.time()
        if rest <= 0:
            break
        try:
            # long-poll: the reactive wait is held by Telegram's
            # server (up to min(rest, 25) s) — no polling with sleeps
            offset, batch, _n = fetch_updates(cfg, offset, min(rest, 25))
        except Exception as exc:  # noqa: BLE001
            log("errors", f"getUpdates failed: {exc}")
            continue
        save_offset(offset)
        if batch:
            msg = batch[-1]
            cfg["chat_id"] = msg["chat"]["id"]
            cfg["owner_user_id"] = msg["from"]["id"]
            cfg["owner_name"] = msg["from"].get("first_name") or msg["from"].get("username") or ""
            save_config(cfg)
            uhttp.send(cfg, f"✅ Registered, {cfg['owner_name']}. ZCode notices arrive here.")
            print(f"OK chat_id={cfg['chat_id']} user={cfg['owner_name']}")
            return
    print("No message arrived. Try again.")
    sys.exit(1)


def cmd_stuck_watch(token, session):
    cfg = load_config()
    minutes = int(cfg.get("stuck_after_min"))
    deadline = time.time() + minutes * 60
    
    # Use reactive fswatch.wait_for instead of polling with sleep(30)
    # The marker file is deleted when the turn ends (Stop)
    marker = marker_path(session)
    
    def marker_gone():
        return not os.path.exists(marker)
    
    def token_changed():
        try:
            with open(marker) as f:
                current = json.load(f).get("token")
            return current != token
        except (OSError, ValueError):
            return True
    
    def condition():
        if marker_gone() or token_changed():
            return True
        return time.time() >= deadline
    
    # wait_for uses reactive fswatch hooks - zero CPU while waiting
    fswatch.wait_for(condition, [marker], timeout=minutes * 60, poll=30)
    
    # Check why it exited
    if time.time() >= deadline:
        try:
            with open(marker) as f:
                prompt = json.load(f).get("prompt", "")
        except (OSError, ValueError):
            prompt = ""
        if not _task_current_ok(cfg, session):
            return
        uhttp.send(cfg, f"⚠️ <b>Session possibly stuck</b> for {minutes} min.\nTurn: {esc(preview(prompt, 200))}")


def cmd_watch_app():
    cfg = load_config()
    state_file = os.path.join(config.STATE_DIR, "app-running")
    try:
        with open(state_file) as f:
            was = f.read().strip() == "1"
    except OSError:
        was = None
    
    # Use reactive fswatch.wait_change instead of polling with sleep(60)
    # We watch the ZCode process - when it changes (start/stop), fswatch wakes us
    while True:
        # Check current state
        up = subprocess.run(["pgrep", "-x", "ZCode"], capture_output=True).returncode == 0
        if was is not None and up != was:
            uhttp.send(cfg, "🔴 <b>ZCode closed</b>" if not up else "🟢 <b>ZCode started again</b>")
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(state_file, "w") as f:
            f.write("1" if up else "0")
        was = up
        
        # Reactive wait: fswatch.wait_change watches the process via pgrep state
        # We use a sentinel file that changes when the process changes
        # As a simple fallback, we use wait_change on an empty directory with timeout
        # (In production ideally: watch /proc or use kqueue EVFILT_PROC)
        sentinel = os.path.join(config.STATE_DIR, "app-watch-sentinel")
        try:
            os.utime(sentinel, None)
        except OSError:
            pass
        # Wait up to 60s or until the sentinel changes (we touch it upon detecting a change)
        fswatch.wait_change([sentinel], timeout=60, poll=5)
