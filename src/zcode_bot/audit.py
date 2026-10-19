"""audit — registro compartido de fallos (F30).

El faltante del incidente del 15/10: cada componente detectaba SU error
y lo mandaba al chat, donde moría. Nadie correlacionaba. Este registro
es el lugar común (state/bot-auditoria.json, flock): una línea por
fallo con (ts, sid, tipo) que el auditor correlaciona para detectar
patrones ("sesión enferma") en el segundo intento, no en el cuarto.

Deps mínimas a propósito (config + state, NI core): utils/http.py lo
importa para registrar sus fallos de envío, y http.py es importado por
todos — un ciclo acá sería sistémico. Paths por atributo en call-time
(regla de la casa): el fixture de tests rebindea config.STATE_DIR y
este módulo lo sigue sin re-import.
"""
import os
import time

from zcode_bot import config
from zcode_bot.state.store import AtomicStore

CAP = 300  # últimos 300 eventos: historia suficiente, archivo chico


def _registro():
    """AtomicStore del registro — path resuelto EN EL MOMENTO."""
    return AtomicStore(os.path.join(config.STATE_DIR, config.ARCH_AUDITORIA))


def registrar(sid, tipo, detalle="", extra=None):
    """Anota un fallo detectado. sid None = fallo global (p.ej. envío).
    extra: dict opcional con campos adicionales (p.ej. la "clave" de
    anti-spam del auditor). Jamás lanza: el registrador está en un camino
    de error — un fallo del registro no puede empeorar el fallo original."""
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
    except Exception:  # noqa: BLE001 — ver arriba: jamás empeora
        pass


def leer():
    """Snapshot del registro (el auditor y los tests)."""
    try:
        q = _registro().read()
        return q if isinstance(q, list) else []
    except Exception:  # noqa: BLE001
        return []
