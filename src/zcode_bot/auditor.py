"""auditor — cross-component invariant watcher (F30).

The immune system that was missing: orphans_check, stuck-watch and
outbox are INTRA-component self-checks; this module watches the
BOUNDARIES as a black box, via files, with no locks and no hot path.
The four invariants came out of real incidents on 15/10:

1. failure correlation → "sick session" on the 2nd identical attempt,
   not the fourth (incident 18:10: 4 /compact and 2 turns with the
   same error).
2. orphan /stop markers → nuclear SIGTERMs the worker (the handler
   CREATES the marker) and the later SIGKILL prevents it from consuming
   it: the marker was stopping the NEXT turn on the rebound (13066,
   18:13).
3. ghost workers → a live run-turn with no registry entry (empty
   registry = /stop, /status and the F15 guard all blind — incident
   15:40).
4. sleeping queue → queued items nobody will pick up (the normal F17
   flow is "Mac busy"; the anomaly is queuing with NO run in flight).
5. orphan CoT cache → vivo=True of a turn that FINISHED with no watcher
   or worker: the watcher died midway and nobody closed it (F51a).

Philosophy: HEALS what is idempotent (consume an orphan marker),
RECOMMENDS what is state-dependent (rotate a sick session, /stop go on
a ghost, /qgo the queue) — it never runs destructive actions on its
own. Anti-spam: one alert per key every `auditor_alerta_cooldown_min`.
"""
import json
import os
import time

from zcode_bot import audit
from zcode_bot import config
from zcode_bot import nuclear as zc_nuclear
from zcode_bot import workers as zc_workers
from zcode_bot.core import _store, _stop_consume, running_turns
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc

# Failure correlation window (10 min: the 18:10/18:12 incident fit
# entirely within 2 min; 10 covers spaced-out retries)
FAILURES_WINDOW_S = 600
# Anti-race for the orphan marker: a spawn may still be in flight
ORPHAN_MARKER_S = 60
# Queue with nobody to pick it up (normal F17 is "Mac busy" and does NOT alert)
IDLE_QUEUE_MIN = 10
# CoT cache: anti-race grace with the watcher's final fetch (a fresh
# heartbeat already skips it; this covers the died-just-before case)
ORPHAN_COT_S = 90

_LAST = [0.0]  # internal throttle: the cadence is set by the knob, not the loop


def run_audit(cfg, now=None, pids_run_turn=None, es_run_turn=None):
    """One pass over the invariants. Runs in the poller's supervisor with
    its own throttle (auditor_cada_s; 0 = off). Each invariant is
    best-effort: if one blows up, the others still run — and NONE of
    them takes down the supervisor (same contract as on_* in hooks)."""
    now = time.time() if now is None else now
    every_s = float(cfg.get("auditor_interval_s") or 0)
    if every_s <= 0:
        return
    if now - _LAST[0] < every_s:
        return
    _LAST[0] = now
    for check in (_failure_correlation, _orphan_markers,
                  _ghost_workers, _idle_queue, _orphan_caches):
        try:
            check(cfg, now, pids_run_turn=pids_run_turn,
                  es_run_turn=es_run_turn)
        except Exception:  # noqa: BLE001 — the auditor never breaks the poller
            pass


def _alert(cfg, key, text, now):
    """Anti-spam: one alert per key every `auditor_alert_cooldown_min`.
    The mark is left in the audit log (type=alert, in the key field;
    audit.leer normalizes the old tipo/clave spelling, so an alert
    recorded by a pre-deploy build still suppresses within cooldown)."""
    cooldown = float(cfg.get("auditor_alert_cooldown_min") or 60) * 60
    for ev in audit.leer():
        if ev.get("type") == "alert" and ev.get("key") == key \
                and now - float(ev.get("ts") or 0) < cooldown:
            return
    audit.registrar(None, "alert", detail=text, extra={"key": key})
    uhttp.send(cfg, text, loud=False)


# ------------------------------------------------------- invariants

def _failure_correlation(cfg, now, pids_run_turn=None, es_run_turn=None):
    """≥ N failures of the same (sid, kind) within the window → "sick
    session". The sid=None bucket groups global failures (sends): the
    429/deadlock incident at 15:47 would have alerted here. Kinds are
    normalized by audit.leer, so failures recorded under the old Spanish
    spellings correlate with the new ones."""
    minimum = int(cfg.get("auditor_failures_min") or 2)
    groups = {}
    for ev in audit.leer():
        if ev.get("type") == "alert":
            continue  # our own alerts are not failures to correlate
        ts = float(ev.get("ts") or 0)
        if ts < now - FAILURES_WINDOW_S:
            continue
        groups.setdefault((ev.get("sid"), ev.get("type")), []).append(ts)
    for (sid, kind), tss in groups.items():
        if len(tss) < minimum:
            continue
        mins = max(1, int((now - min(tss)) / 60))
        if sid:
            key = "failures|%s|%s" % (sid, kind)
            text = ("⚠️ sess_%s: %d failures of «%s» in %d min — sick "
                     "session. Suggestion: /task to another conversation "
                     "or wait for the rotation."
                     % (sid[-8:], len(tss), kind, mins))
        else:
            key = "failures|global|%s" % kind
            text = ("⚠️ %d failures of «%s» in %d min on the sends — "
                    "check connectivity or /ping." % (len(tss), kind, mins))
        _alert(cfg, key, text, now)


def _orphan_markers(cfg, now, pids_run_turn=None, es_run_turn=None):
    """/stop marker with no owner → consume it (silent healing). The
    owner is the sid's worker: alive in the registry OR an active turn.
    The 60s threshold covers the race with a spawn in flight. The
    filename is the sanitized sid, and real sids contain no characters
    the sanitizer touches → filename == sid."""
    d = config.stop_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    active = running_turns()
    for name in names:
        path = os.path.join(d, name)
        try:
            if now - os.path.getmtime(path) <= ORPHAN_MARKER_S:
                continue  # fresh: a spawn may still be in flight
        except OSError:
            continue
        if name in active:
            continue
        if zc_workers._workers().get(name) is not None:
            continue  # live worker: legitimate owner
        _stop_consume(name)
        audit.registrar(name, "orphan-marker",
                        "consumed with no owner (empty registry, no turn)")


def _ghost_workers(cfg, now, pids_run_turn=None, es_run_turn=None):
    """run-turn ALIVE with no registry entry → recommend /stop go. With
    an empty registry, /stop, /status and the stuck-watch's F15 guard go
    blind (incident 15:40) — this makes it visible."""
    pids_run_turn = pids_run_turn or (lambda: zc_nuclear._nuc_pids(
        ["-f", "zcode-telegram.py --run-turn"]))
    es_run_turn = es_run_turn or zc_workers._run_turn_ok
    registered = set()
    for _sid, w in zc_workers._workers().items():
        try:
            registered.add(int(w.get("pid") or 0))
        except (TypeError, ValueError):
            continue
    ghosts = [p for p in pids_run_turn()
              if p != os.getpid() and p not in registered
              and es_run_turn(p)]
    if not ghosts:
        return
    text = ("🧟 Worker with no registry entry (pid %s): /stop, /status and "
             "the stuck-watch cannot see it — /stop go to finish it off, or "
             "/nuclear if it persists."
             % ", ".join(str(p) for p in ghosts))
    _alert(cfg, "ghost|%s" % ",".join(str(p) for p in ghosts),
           text, now)


def _idle_queue(cfg, now, pids_run_turn=None, es_run_turn=None):
    """Queued with no worker and no run in flight for > 10 min → nobody
    will pick it up (F17's "Mac busy" case has a run in flight and does
    NOT alert here). Legacy items without ts do not alert (no honest
    data, no notice)."""
    q = _store(config.QUEUE_FILE).read()
    if not isinstance(q, dict):
        return
    active = running_turns()
    for sid, items in q.items():
        if not isinstance(items, list) or not items:
            continue
        if zc_workers._workers().get(sid) is not None:
            continue  # live worker: it will pick the queue up when it finishes
        if sid in active:
            continue  # Mac busy: F17, normal behavior
        tss = [float(i.get("ts") or 0) for i in items
               if isinstance(i, dict) and i.get("ts")]
        if not tss:
            continue  # legacy without ts
        mins = (now - min(tss)) / 60.0
        if mins < IDLE_QUEUE_MIN:
            continue
        title = ((items[0].get("act") or {}).get("title") or sid[-8:]) \
            if isinstance(items[0], dict) else sid[-8:]
        text = ("📬 %d queued message%s for «%s» have gone %d min with no "
                 "run picking them up — /qgo to launch them or /task to the "
                 "conversation." % (len(items),
                                    "s" if len(items) != 1 else "",
                                    esc(str(title)), int(mins)))
        _alert(cfg, "idle-queue|%s" % sid, text, now)


def _orphan_caches(cfg, now, pids_run_turn=None, es_run_turn=None):
    """F51a — CoT cache with vivo=True of a turn that FINISHED with no
    watcher (heartbeat expired) and no worker: the watcher died midway
    (kill -9/OOM) and nobody closed the cache. Idempotent healing (the
    house philosophy: close_cache keeps boxes/labels/ph/phs — the
    taps keep working —, it only clears vivo and records the reason).
    Without this the JSON would claim vivo forever."""
    from zcode_bot import reasoning as zc_rz  # late: workers imports it
    try:
        names = os.listdir(config.STATE_DIR)
    except OSError:
        return
    active = running_turns()
    for name in names:
        if not (name.startswith("cot-") and name.endswith(".json")):
            continue
        base = name[4:-5]
        sid, _, t0s = base.rpartition("-")
        if not sid or not t0s.isdigit():
            continue
        path = os.path.join(config.STATE_DIR, name)
        try:
            if now - os.path.getmtime(path) <= ORPHAN_COT_S:
                continue  # fresh: the live one may be in its final fetch
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not (d.get("live") or d.get("vivo")) \
                or (d.get("done") or d.get("finalizado")):
            continue  # already closed or never opened
        if sid in active:
            continue  # the turn is still running: closing it would silence the fallback
        if zc_workers._workers().get(sid) is not None:
            continue  # owning worker: its orphans_check closes it (F47d)
        if zc_rz.watcher_alive(sid):
            continue  # heartbeating: legitimate owner between writes
        if zc_rz.close_cache(sid, int(t0s), error="watcher died"):
            audit.registrar(sid, "orphan-cot",
                            "cache live with no watcher or turn — closed")
