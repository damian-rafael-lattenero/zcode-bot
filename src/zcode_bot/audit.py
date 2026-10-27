"""audit — shared failure log (F30).

What was missing in the 15/10 incident: each component detected ITS own
error and sent it to the chat, where it died. Nobody correlated. This
log is the common place (state/bot-audit.json, flock): one line per
failure with (ts, sid, type) that the auditor correlates to detect
patterns ("sick session") on the second attempt, not the fourth.

P6: the file name and the entry keys/values were Spanish (bot-auditoria
.json; tipo/detalle/clave; kinds like "envio"). New writes are
English-only; read() NORMALIZES the old shape when returning so every
reader (the auditor's correlation, tests) sees one shape forever —
entries age out by CAP anyway. While the NEW file is still absent, the
OLD one is read (one-time continuity across the deploy).
Deliberately minimal deps (config + state, NOT core): utils/http.py
imports it to log its send failures, and http.py is imported by
everyone — a cycle here would be systemic. Paths by attribute at
call-time (house rule): the test fixture rebinds config.STATE_DIR and
this module follows it without a re-import.
"""
import os
import time
import traceback
from typing import Any, Dict, List, Optional

from zcode_bot import config
from zcode_bot.schemas import AuditEntry
from zcode_bot.utils.logging import pwlog
from zcode_bot.state.store import AtomicStore

CAP = 300  # last 300 events: enough history, small file

# Legacy Spanish kind VALUES → English (P6): correlation must match a
# failure recorded by a pre-deploy process against the new spelling.
LEGACY_KINDS = {
    "turno-muerto": "dead-turn",  # noqa: spanish — legacy persisted kind values (LEGACY_KINDS, permanent)
    "marker-huerfano": "orphan-marker",  # noqa: spanish — legacy kind value
    "cola-dormida": "idle-queue",  # noqa: spanish — legacy kind value
    "cot-huerfano": "orphan-cot",  # noqa: spanish — legacy kind value
    "alerta": "alert",
    "envio": "send",
    "espectador": "spectator",
}
# Legacy anti-spam key WORDS (the auditor's "key" field is built as
# "namespace|scope|kind") → English namespaces. The kind segment goes
# through LEGACY_KINDS below.
LEGACY_KEY_WORDS = {
    "fallos": "failures",
    "cola": "idle-queue",  # noqa: spanish — legacy key word (LEGACY_KEY_WORDS, permanent)
    "fantasma": "ghost",
}


def norm_kind(kind: str) -> str:
    """One spelling for one failure kind, old or new."""
    return LEGACY_KINDS.get(kind, kind)


def norm_key(key: Any) -> Any:
    """One spelling for one anti-spam key, old or new segments."""
    if isinstance(key, str):
        return "|".join(LEGACY_KEY_WORDS.get(seg, norm_kind(seg))
                        for seg in key.split("|"))
    return key


def _registro():
    """AtomicStore for the log — path resolved AT CALL TIME."""
    return AtomicStore(os.path.join(config.STATE_DIR, config.AUDIT_FILE))


def _registro_legacy():
    """The pre-P6 file (state/bot-auditoria.json) — read-only fallback."""
    return AtomicStore(os.path.join(config.STATE_DIR, config.AUDIT_FILE_LEGACY))


def _normalize(ev):
    """Old entry shape (tipo/detalle/clave, Spanish kinds/keys) → the
    new one. New entries pass through untouched; never raises."""
    if not isinstance(ev, dict) or "ts" in ev and "type" in ev \
            and "clave" not in ev:
        return ev
    out = dict(ev)
    if "type" not in out:
        out["type"] = norm_kind(out.pop("tipo", None))
    if "detail" not in out:
        out["detail"] = out.pop("detalle", None)
    if "clave" in out:
        out["key"] = norm_key(out.pop("clave"))
    elif "key" in out:
        out["key"] = norm_key(out["key"])
    return out


def record(sid: Optional[str], kind: str, detail: str = "",
           extra: Optional[Dict[str, Any]] = None) -> None:
    """Records a detected failure. sid None = global failure (e.g. send).
    extra: optional dict with additional fields (e.g. the auditor's
    anti-spam "key"). Never raises: the recorder sits on an error path —
    a logging failure must not worsen the original failure."""
    try:
        def fn(q):
            q = q if isinstance(q, list) else []
            ev = {"ts": round(time.time(), 1), "sid": sid,
                  "type": kind, "detail": str(detail)[:200]}
            if isinstance(extra, dict):
                ev.update(extra)
            q.append(ev)
            return q[-CAP:]
        _registro().update_cross_process(fn, [])
    except Exception:  # noqa: BLE001 — see above: never makes it worse
        pwlog("audit record FAILED: the audit trail lost an entry:",
              traceback.format_exc())


def read() -> List[AuditEntry]:
    """Snapshot of the log (the auditor and the tests): entries in the
    NEW shape — old-shape entries are normalized on the way out. If the
    new file does not exist yet (fresh deploy), the legacy one is read
    (one-time continuity; it stops being written and ages out)."""
    try:
        q = _registro().read()
        if not isinstance(q, list) and not os.path.exists(
                os.path.join(config.STATE_DIR, config.AUDIT_FILE)):
            q = _registro_legacy().read()
        return [_normalize(e) for e in q] if isinstance(q, list) else []
    except Exception:  # noqa: BLE001  # silent-by-design: probe/read, the except is the verdict
        return []