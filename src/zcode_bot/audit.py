"""audit — shared failure log (F30).

What was missing in the 15/10 incident: each component detected ITS own
error and sent it to the chat, where it died. Nobody correlated. This
log is the common place (state/bot-auditoria.json, flock): one line per
failure with (ts, sid, tipo) that the auditor correlates to detect
patterns ("sick session") on the second attempt, not the fourth.

Deliberately minimal deps (config + state, NOT core): utils/http.py
imports it to log its send failures, and http.py is imported by
everyone — a cycle here would be systemic. Paths by attribute at
call-time (house rule): the test fixture rebinds config.STATE_DIR and
this module follows it without a re-import.
"""
import os
import time

from zcode_bot import config
from zcode_bot.state.store import AtomicStore

CAP = 300  # last 300 events: enough history, small file


def _registro():
    """AtomicStore for the log — path resolved AT CALL TIME."""
    return AtomicStore(os.path.join(config.STATE_DIR, config.ARCH_AUDITORIA))


def registrar(sid, tipo, detalle="", extra=None):
    """Records a detected failure. sid None = global failure (e.g. send).
    extra: optional dict with additional fields (e.g. the auditor's
    anti-spam "clave"). Never raises: the recorder sits on an error path —
    a logging failure must not worsen the original failure."""
    try:
        def fn(q):
            q = q if isinstance(q, list) else []
            ev = {"ts": round(time.time(), 1), "sid": sid,
                  "tipo": tipo, "detalle": str(detalle)[:200]}
            if isinstance(extra, dict):
                ev.update(extra)
            q.append(ev)
            return q[-CAP:]
        _registro().update_cross_process(fn, [])
    except Exception:  # noqa: BLE001 — see above: never makes it worse
        pass


def leer():
    """Snapshot of the log (the auditor and the tests)."""
    try:
        q = _registro().read()
        return q if isinstance(q, list) else []
    except Exception:  # noqa: BLE001
        return []
