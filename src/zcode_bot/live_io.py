"""live_io — the runtime's LIVE-CoT sidecar reader (F73).

The fork (ZCode 1e4f7413) flushes mid-request reasoning to
live-io-<session>.jsonl next to the model-io rollout: first partial at
once, then at most one per second, tails capped, one request per file
(rewritten at its start) and REMOVED when the request's full record
lands. This module is the bot's door to it: latest(sid) → the freshest
partial — real chars in flight, the reasoning tail, how long it has
been thinking. Pure file: no runtime, no client, no protocol — the
attach, the Mac beat and the agent watchers all read the same door.
Without the fork's build there is simply no sidecar: latest() → None
and every consumer degrades to its F70 behavior.
"""
import datetime
import json
import os
import time
from typing import Any, Dict, Optional

from zcode_bot import config


def sidecar_path(sid: str) -> str:
    """live-io-<session>.jsonl next to the session's model-io rollout
    (same sanitized segment — derived from rollout_path, never rebuilt)."""
    return config.rollout_path(sid).replace("model-io-", "live-io-", 1)


def _epoch_ms(iso: Any) -> Optional[float]:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


def latest(sid: str, max_age: float = 900.0,
           read: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """The sidecar's LAST complete partial, fresh enough to trust. None
    = nothing in flight (completed requests remove the file — its
    existence IS the «thinking right now» signal) or a stale corpse
    from a crash (the next request's first flush rewrites it anyway)."""
    p = sidecar_path(sid)
    try:
        if read is not None:
            tail = read(p)
        else:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                f.seek(max(0, end - 32 * 1024))
                tail = f.read().decode("utf-8", errors="replace")
        if not tail.endswith("\n"):
            cut = tail.rfind("\n")
            if cut < 0:
                return None
            tail = tail[:cut]
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        if not lines:
            return None
        d = json.loads(lines[-1])
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("type") != "live_io_partial":
        return None
    ts = _epoch_ms(d.get("updatedAt"))
    if ts is not None and (time.time() * 1000.0 - ts) / 1000.0 > max_age:
        return None
    return d


def chars_line(d: Dict[str, Any]) -> str:
    """The one-line digest for beats and heartbeats: REAL chars in
    flight (the store's in-progress part lags and settles in bulk —
    this is the runtime's own accumulator)."""
    n = int(d.get("reasoningChars") or 0)
    t = int(d.get("textChars") or 0)
    if n >= 1000:
        return "in-flight ~%dk chars" % (n // 1000)
    if n or t:
        return "in-flight %d chars" % (n or t)
    return ""


def tail_of(d: Dict[str, Any], cap: int = 600) -> str:
    """The reasoning tail (what it is thinking NOW), capped for a live
    preview inside the filling box's message."""
    return str(d.get("reasoningTail") or "")[-cap:]
