"""core — the bot's shared seam (F4b).

Breaking up the gateway surfaced 10 dependency cycles across the
domains (reports ↔ workers ↔ hooks ↔ commands...). This module is the
ROOT CUT: the state and session functions everyone shares live here,
and core depends on no other bot domain.

House rule (F1e/F4a): paths are ALWAYS resolved by attribute
(config.STATE_DIR) at call-time — never baked in at import time.
"""
import datetime
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, NamedTuple, Optional

from zcode_bot import config
from zcode_bot import state as zc_state
from zcode_bot import concurrency as zc_conc
from zcode_bot import proto as zc_proto
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import cli_logs as zc_logs
from zcode_bot import fswatch
from zcode_bot.models import MODEL_SEL
from zcode_bot.utils.validation import first
from zcode_bot.utils.formatting import preview
from zcode_bot.utils.logging import log


def _store(name):
    """AtomicStore for a state file (path resolved AT CALL TIME —
    so tests can patch config.STATE_DIR after import)."""
    return zc_state.AtomicStore(os.path.join(config.STATE_DIR, name))


# F16: worker registry in a FILE (state/bot-workers.json, flock) —
# the same view for the poller and the worker processes; a poller restart
# does not wipe in-flight turns (before: daemon threads with in-memory state).
# F16: ❓ questions via files (state/bot-q/) — the chat answer
# crosses from the poller to the worker process with no shared memory.
# F4a: accessors with the path resolved AT CALL TIME (cached by path:
# test fixture and production never collide). They used to be globals that
# baked in the path at import — the test fixture had to rebind them.
_WORKERS_CACHE = {}
_QUESTIONS_CACHE = {}


def _workers():
    path = os.path.join(config.STATE_DIR, config.WORKERS_FILE)
    w = _WORKERS_CACHE.get(path)
    if w is None:
        w = _WORKERS_CACHE[path] = zc_conc.WorkersRegistry(path)
    return w


def _questions():
    path = os.path.join(config.STATE_DIR, "bot-q")
    q = _QUESTIONS_CACHE.get(path)
    if q is None:
        q = _QUESTIONS_CACHE[path] = zc_conc.QuestionFiles(path)
    return q


# ------------------------------------------------------------- entities

@dataclass
class BotSession:
    """The bot's active conversation (state/bot-session.json).

    F22: model and mode SURVIVE the /task switch — they only go into
    the JSON if they exist (to_dict preserves the file's historical shape).
    Compat mapping (get/keys/__getitem__): old consumers
    (act.get("sid"), dict(act, mode=...)) keep working while they migrate
    to attributes.
    """
    sid: str = ""
    title: str = ""
    ws: str = ""
    model: Optional[Dict[str, Any]] = None
    mode: Optional[str] = None
    # F7++ (deep audit 20/10): the mode ALREADY applied to the runtime —
    # prompt_worker writes it after setMode so the next cold turn can
    # skip it, but from_dict DISCARDED it: the skip_mode optimization
    # was dead and every turn paid session/setMode.
    mode_set: Optional[str] = None

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(sid=str(d.get("sid") or ""),
                   title=str(d.get("title") or ""),
                   ws=str(d.get("ws") or ""),
                   model=d.get("model") if isinstance(d.get("model"), dict) else None,
                   mode=d.get("mode") if d.get("mode") else None,
                   mode_set=d.get("mode_set") if d.get("mode_set") else None)

    def to_dict(self):
        d = {"sid": self.sid, "title": self.title, "ws": self.ws}
        if self.model is not None:
            d["model"] = self.model
        if self.mode is not None:
            d["mode"] = self.mode
        if self.mode_set is not None:
            d["mode_set"] = self.mode_set
        return d

    # --- compat mapping (F4b transition; new modules use attributes)
    def keys(self):
        return self.to_dict().keys()

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def get(self, k, default=None):
        return getattr(self, k, default)


class WaitReply(NamedTuple):
    """The owner's answer to a wait (wait_reply): kind ∈ yes|no|text,
    text = what was typed. NamedTuple because the historical return was
    the (kind, text) tuple — unpackings and test stubs stay intact."""
    kind: Optional[str]
    text: Optional[str]


# ------------------------------------------------------------ active session

def active_bot_session():
    return BotSession.from_dict(_store(config.SESSION_FILE).read())


def set_active_bot_session(d):
    if isinstance(d, BotSession):
        d = d.to_dict()
    try:
        _store(config.SESSION_FILE).write(d)
    except OSError as e:
        log("errors", "set_active_bot_session: %s" % e)


def active_workspace_path():
    """Workspace shown by the app window (setting.json:
    lastWorkspaceSession[lastActiveTabIndex])."""
    try:
        s = json.load(open(os.path.join(config.BASE, "v2", "setting.json")))
        lws = s.get("lastWorkspaceSession") or []
        i = s.get("lastActiveTabIndex")
        if lws and isinstance(i, int) and 0 <= i < len(lws) and isinstance(lws[i], dict):
            return lws[i].get("workspacePath")
        if lws and isinstance(lws[0], dict):
            return lws[0].get("workspacePath")
    except (OSError, ValueError):
        pass
    return None


def session_title_from_db(sid):
    """A session's title (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.session_title(config.BASE, sid)


# --------------------------------------- shared state reads

def running_turns(max_age=7200):
    """Sessions with a turn running right now, per the CLI log
    (replay of started/completed/failed over the last 2 logs).
    F51b: turn.cancelled also releases — it does not show up in the logs
    today (the app cancels as turn.failed with error «Turn was cancelled»,
    seen 20/10 14:55), but if it is ever emitted and is missing here, the
    session would stay "busy" for 2 h and phone messages would queue up
    for no reason."""
    lim = time.time() - max_age
    out = {}
    for ts, sid, ev, _d in zc_logs.iter_events(
            config.BASE, needles=("turn.",), since_epoch=lim,
            last_files=2, tail_bytes=0):
        if ev == "turn.started":
            out[sid] = ts
        elif ev in ("turn.completed", "turn.failed", "turn.cancelled"):
            out.pop(sid, None)
    return out


def _queue_peek(sid):
    q = _store(config.QUEUE_FILE).read()
    return (q.get(sid) or []) if isinstance(q, dict) else []


def _queue_sids_with_items():
    q = _store(config.QUEUE_FILE).read()
    if not isinstance(q, dict):
        return []
    return [sid for sid, lst in q.items() if lst]


def _hook_sid(payload):
    sid = first(payload, "session_id", "sessionId")
    return str(sid) if sid else None


def _task_current_ok(cfg, sid):
    """F15 — anti-noise filter: a session may push to the chat only if it
    is the current task (bot-session.json read LIVE on every send: a
    /task switch cuts the stream mid-turn). Direct replies to the user's
    commands/messages do NOT go through here. With no active sid there is
    no task push (forces a choice via /task)."""
    if not cfg.get("only_active_task"):
        return True
    if not sid:
        return False
    return sid == active_bot_session().get("sid")


def _worker_owned(sid):
    """F15 — session with a bot worker running: the worker ALREADY reports
    its turn (live box, CoT, ✅/⏹️). The ▶️/✅/🔐/❓/🟢 hooks for that session
    would only duplicate (verified double-stream: headless turn payloads
    with HEADLESS_GUARD firing UserPromptSubmit+Stop on top of the
    box). A hook with wait_reply would also compete for the offset."""
    return bool(sid) and _workers().get(sid) is not None


# ------------------------------------------------- model/mode selection

def _cur_sel(act):
    """The active conversation's current model selection (sane dict with
    provider/modelId/options.reasoningLevel)."""
    sel = dict(MODEL_SEL)
    sel["options"] = dict(MODEL_SEL.get("options") or {})
    if isinstance(act.get("model"), dict):
        sel.update({k: v for k, v in act["model"].items() if k != "options"})
        sel["options"] = dict(act["model"].get("options") or sel["options"])
    return sel


def _set_sel(act, model_id=None, thinking=None):
    """Changes the active session's model or thinking, PRESERVING the other."""
    sel = _cur_sel(act)
    if model_id:
        sel["modelId"] = model_id
    if thinking:
        sel["options"] = dict(sel.get("options") or {}, reasoningLevel=thinking)
    set_active_bot_session(dict(act, model=sel))


def _model_snapshot(sid):
    """The session's latest runtime/model_selection (raw JSON data) or None
    (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.model_selection(config.BASE, sid)


def _restore_model(sid, snap):
    """Restores the selection to its previous state (or deletes it if there
    was none): the bot's zai-bot setModel must NOT be left stuck on app
    sessions (their registry does not know zai-bot and the picker hangs)."""
    import sqlite3
    try:
        db = sqlite3.connect(os.path.join(config.BASE, "cli", "db", "db.sqlite"))
        if snap is None:
            db.execute("DELETE FROM session_entry WHERE session_id=? "
                       "AND type='runtime/model_selection'", (sid,))
        else:
            db.execute("UPDATE session_entry SET data=? WHERE session_id=? "
                       "AND type='runtime/model_selection'", (snap, sid))
        db.commit()
        db.close()
    except sqlite3.Error as e:
        log("errors", "_restore_model(%s): %s — the model selection may "
            "have been left in zai-bot" % (sid, e))


# ------------------------------------------------------ CLI runtime

def transcript_mb(sid):
    """MB of the session's transcript (rollout model-io jsonl): what EVERY
    cold request pays (resume/setModel/setMode return the complete
    history). compact does NOT shrink it (15/10: 4 successful compacts
    and the resume still sat at 27.5MB — compact shrinks the model's
    CONTEXT, not the history)."""
    p = config.rollout_path(sid)
    try:
        return os.path.getsize(p) / (1024.0 * 1024.0)
    except OSError:
        return 0.0


def _timeout_by_mb(sid):
    """Wait ceiling for resume/setModel/setMode. Every request against an
    existing session returns the COMPLETE history (F7++), so the ceiling
    scales with the transcript. Incident 18:10 (15/10): with a fixed
    30/60s timeout, setModel/setMode timed out on a 25MB session
    (~30s per request under load) → the turn died silently and in a
    LOOP (mode_set is only recorded after a successful _proto_ready,
    so the next turn paid it all again). Math: 60s base +
    4s/MB (~1.1MB/s measured, 3x margin for load), cap 300s. It is a
    CEILING, not a wait: a small session still answers in seconds."""
    return min(300, 60 + int(transcript_mb(sid) * 4))


def _last_line_with(p, needle, limit=8 << 20):
    """Last COMPLETE line of the file containing `needle`, reading the
    tail with a growing window (64KB → ×8 up to `limit`). A model-io jsonl
    record weighs as much as the whole context (200KB+ in a large
    session): with a fixed 64KB tail the only "line" was a truncated
    JSON and the parse failed exactly in the sessions where the data
    matters (16/10: _context_ktokens returned None with 180k real
    tokens). None if no complete line contains the needle."""
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        total = f.tell()
        n = 65536
        while True:
            n = min(n, total)
            f.seek(total - n)
            lines = f.read().decode("utf-8", "replace").splitlines()
            if n < total:
                lines = lines[1:]  # the first one starts mid-record
            hits = [l for l in lines if needle in l]
            if hits:
                return hits[-1]
            if n >= total or n >= limit:
                return None
            n *= 8


def last_used(sid):
    """Last rollout request with measured usage: (ktokens, epoch,
    source). ktokens = what the model saw in that request (includes
    cache read); epoch = the record's completedAt (0.0 if missing);
    source = the runtime's querySource ('main_turn', 'compact', ...).
    None without data (no file, no usage — e.g. a new session — or a
    broken jsonl)."""
    p = config.rollout_path(sid)
    try:
        line = _last_line_with(p, '"usage"')
        if not line:
            return None
        reg = json.loads(line)
        d = reg.get("response") or {}
        u = d.get("usage") or (((d.get("providerMetadata") or {})
                                .get("anthropic") or {}).get("usage") or {})
        if "inputTokens" in u:  # canonical form (includes cache read)
            ktok = u["inputTokens"] / 1000.0
        elif "input_tokens" in u:  # provider form: cache_read comes separately
            ktok = (u["input_tokens"]
                    + (u.get("cache_read_input_tokens") or 0)) / 1000.0
        else:
            return None
        epoch = 0.0
        ts = reg.get("completedAt")
        if isinstance(ts, str) and ts:
            try:
                epoch = datetime.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                epoch = 0.0
        return (ktok, epoch, str(reg.get("querySource") or ""))
    except (OSError, ValueError, TypeError):
        return None


def _context_ktokens(sid):
    """REAL context tokens in thousands: the latest usage in the model-io
    jsonl (what the model saw in the most recent request). None if there
    is no data (no file or no usage — e.g. a new session)."""
    u = last_used(sid)
    return u[0] if u else None


def _compact_timeout(sid):
    """Wait ceiling for session/compact — Damian's linear rule calibrated
    against measurements: 1.2 s per 1k context tokens (measured 15/10:
    ~135k tokens → 103s real ≈ 0.76 s/ktok; 1.2 leaves ~1.6x of margin).
    100k → 120s, 300k → 360s; base 300 (the usual default, 3x the
    largest measured compact), cap 600 (10 min)."""
    ktok = _context_ktokens(sid)
    if not ktok:
        return 300
    return min(600, max(300, int(ktok * 1.2)))


def _proto_ready(c, sid, model_sel=None, mode=None, skip_model=False,
                 skip_mode=False):
    """resume + setModel + setMode. The bot ALWAYS forces an explicit mode:
    no saved mode → yolo (design decision: remote prompts run without
    permission dialogs; a headless build/edit session is left with nobody
    to approve and the agent ends up asking to "reiniciá ZCode" [restart
    ZCode]). Returns None or an error msg.
    F7+: skip_model/skip_mode skip the NO-OP sets. Every request against
    a large session returns the COMPLETE history in the response
    (measured on the F4+F7 session: 26MB / 3640 messages ≈ 30s PER
    request — the "takes a minute to start") — all three requests use
    the per-MB ceiling (incident 18:10: the fixed 30/60s ones timed out
    and the turn died in a loop). The model is validated against the
    authoritative sqlite (runtime/model_selection); the mode against
    mode_set, the record of what THIS bot last set."""
    t_req = _timeout_by_mb(sid)
    r = c.request("session/resume", {"sessionId": sid}, timeout=t_req)
    if e := zc_proto.proto_err(r, "resume"):
        return e
    if not skip_model:
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": model_sel or MODEL_SEL},
                      timeout=t_req)
        if e := zc_proto.proto_err(r, "setModel"):
            return e
    if not skip_mode:
        r = c.request("session/setMode",
                      {"sessionId": sid, "mode": mode or "yolo"},
                      timeout=t_req)
        if e := zc_proto.proto_err(r, "setMode"):
            return e
    return None


def _create_session(c, ws):
    """session/create; returns (sid, None) or (None, error_msg). The
    session lives as long as the client: create it inside the turn's
    worker."""
    r = c.request("session/create",
                  {"workspace": {"workspaceKey": ws, "workspacePath": ws}},
                  timeout=60)
    if e := zc_proto.proto_err(r, "create"):
        return None, e
    session = ((r.get("result") or {}).get("session")) or {}
    sid = session.get("sessionId") or session.get("id")
    if not sid:
        return None, "❌ create: no sessionId in the reply"
    return sid, None


def _log_watch_paths():
    """Paths to watch for CLI-written events: the same files iter_events
    scans (last 2) + the dir (rotation)."""
    d = os.path.join(config.BASE, "cli", "log")
    out = [d]
    try:
        logs = sorted((f for f in os.listdir(d)
                       if f.startswith("zcode-") and f.endswith(".jsonl")),
                      reverse=True)
        out.extend(os.path.join(d, f) for f in logs[:2])
    except OSError:
        pass
    return out


def _wait_cli_event(sid, t0, events, timeout=200):
    """REACTIVE+TTL wait on the CLI log for an event from `sid` with
    ts >= t0: fswatch.wait_for sleeps in the kernel until the log
    changes (same paths as _turn_end_poll) and re-checks the predicate.
    Returns the event dict (the LAST match) or None when the TTL
    expires."""
    matches = []

    def _has_event():
        matches.clear()
        for _ts, _s, _ev, d in zc_logs.iter_events(
                config.BASE, needles=tuple(events), sids={sid}, since_epoch=t0,
                last_files=1, tail_bytes=0):
            matches.append(d)
        return bool(matches)

    if not fswatch.wait_for(_has_event, _log_watch_paths(), timeout):
        return None
    return matches[-1]


# ------------------------------------------------------------ last prompt

def _last_prompt_path():
    return os.path.join(config.STATE_DIR, "last-prompt.json")


def _save_last_prompt(act, text):
    if isinstance(act, BotSession):
        act = act.to_dict()  # the store persists flat dicts
    try:
        _store("last-prompt.json").write({"act": act, "text": text})
    except OSError:
        pass


def _load_last_prompt():
    d = _store("last-prompt.json").read()
    if isinstance(d, dict) and d.get("text"):
        return d.get("act") or {}, str(d["text"])
    return None, None


# ------------------------------------------- /stop via marker (F16)

def _stop_file(sid):
    d = config.stop_dir()
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sid or "ses")
    return os.path.join(d, safe)


def _stop_request(sid):
    """/stop: the poller drops the order; the worker sees it in its loop
    (~2 s) and stops ITS turn with its own runtime (the 'real stop':
    only the owning runtime sees its turns — the shared ProtoClient died
    with the threads)."""
    try:
        with open(_stop_file(sid), "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _stop_requested(sid):
    return bool(sid) and os.path.exists(_stop_file(sid))


def _stop_consume(sid):
    try:
        os.remove(_stop_file(sid))
    except OSError:
        pass


# ----------------------------------------------- permissions: dedupe/replay

def _perm_key(sid, name, inp):
    """Stable fingerprint of a permission request (for dedupe/replay)."""
    try:
        body = json.dumps(inp, sort_keys=True, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        body = preview(inp, 500)
    return "%s|%s|%s" % (sid or "?", name,
                         hashlib.sha1(body.encode("utf-8")).hexdigest()[:16])


def _perm_remember(key, decision):
    """Records the decision for replays (the CLI sometimes re-fires the
    same PermissionRequest; see the 'apruebo y me lo pide otra vez'
    ["I approve and it asks me again"] incident from 13/10)."""
    def fn(d):
        d = d if isinstance(d, dict) else {}
        d[key] = {"ts": time.time(), "decision": decision}
        # prune: keep the latest 50
        if len(d) > 50:
            for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:-50]:
                d.pop(k, None)
        return d
    try:
        _store("bot-perm.json").update_cross_process(fn, {})
    except OSError:
        pass


_PERM_REPLAY_S = 600  # same fingerprint within <10 min → same decision, right away


def _perm_replay(key):
    d = _store("bot-perm.json").read() or {}
    ent = d.get(key)
    if ent and time.time() - ent.get("ts", 0) < _PERM_REPLAY_S:
        return ent.get("decision")
    return None
