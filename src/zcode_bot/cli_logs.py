"""cli_logs — parameterizable scanner for the CLI's JSONL logs.

Pattern: Strategy. Unifies the 4 duplicated scan loops of the monolith
(_turn_ends_since, _compactions, _wait_cli_event, _turn_end_poll) into
a single iteration configurable by filters.

Logs live in <base>/cli/log/zcode-YYYY-MM-DD.jsonl with JSON events per
line: {"timestamp": ISO-8601-UTC, "event": "...", "sessionId": "...",
"context": {...}}.
"""
import datetime
import glob
import json
import os


def _parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


LOG_TAIL = 512 * 1024  # scan tail per CLI log: records weigh
# as much as the context (F31), the tail grows with the window


def iter_events(base, needles=(), sids=None, since_epoch=None,
                last_files=2, tail_bytes=LOG_TAIL,
                include_subagents=False):
    """Iterates events from the last N CLI logs, already parsed and
    filtered, in per-file chronological order (old→new).

    needles: tuple of event prefixes ("turn.", "compact.completed").
    sids: set of sessionIds of interest (None = all, no subagents).
    since_epoch: minimum ts (epoch seconds, with 1 s of tolerance).
    tail_bytes: only the tail of the last file is read (live turns sit
    at the end; reading it whole is O(history)).
    include_subagents: F70 — sess_subagent_* sessions carry their own
    model.* events (request started/completed, retries): the agents'
    life signs. Skipped by default (house callers reason about the
    PARENT session).
    """
    files = sorted(glob.glob(os.path.join(base, "cli", "log", "zcode-*.jsonl")))
    for path in files[-last_files:] if last_files else files:
        is_last = (path == files[-1]) if files else False
        try:
            if is_last and tail_bytes:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - tail_bytes))
                    f.readline()  # discard a partial trailing line
                    data = f.read().decode("utf-8", errors="replace")
            else:
                with open(path, errors="replace") as f:
                    data = f.read()
        except OSError:
            continue
        for line in data.splitlines():
            if not any(n in line for n in needles):
                continue  # cheap filter BEFORE the json.loads
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ev = str(d.get("event") or "")
            if needles and not any(ev.startswith(n) for n in needles):
                continue
            sid = str(d.get("sessionId") or "")
            if sid.startswith("sess_subagent") and not include_subagents:
                continue
            if sids is not None and sid not in sids:
                continue
            ts = _parse_ts(d.get("timestamp"))
            if ts is None:
                continue
            if since_epoch is not None and ts < since_epoch - 1:
                continue
            yield ts, sid, ev, d


def tool_in_flight(sid, now=None, iter_ev=None, max_age=3600.0,
                   base=None):
    """F78 — the tool RUNNING right now on a session, from the log's
    truth: tool.call.started / tool.call.completed. The store settles a
    tool part only when it COMPLETES, so mid-run — a long program —
    only the log knows (verified 27/10: a 68 s Bash, the status line
    blinking on the Mac, nothing anywhere the bot reads). →
    (toolName, started_epoch) of the call whose started has no
    completed after it; None when no tool runs."""
    it = iter_ev or iter_events
    if base is None:
        from zcode_bot import config   # lazy: the scanner stays pure
        base = config.BASE
    t = now if now is not None else datetime.datetime.now().timestamp()
    name, started = None, None
    try:
        events = it(base, needles=("tool.call.",), sids={sid},
                    since_epoch=t - max_age, last_files=1, tail_bytes=0)
    except OSError:
        return None
    for ts, _sid, ev, d in events:
        if ev == "tool.call.started":
            name = str((d.get("context") or {}).get("toolName") or "")
            started = ts
        elif ev == "tool.call.completed":
            name, started = None, None
    return (name, started) if name and started is not None else None
