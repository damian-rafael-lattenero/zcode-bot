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
VENTANA_FALLOS_S = 600
# Anti-race for the orphan marker: a spawn may still be in flight
MARKER_HUERFANO_S = 60
# Queue with nobody to pick it up (normal F17 is "Mac busy" and does NOT alert)
COLA_DORMIDA_MIN = 10
# CoT cache: anti-race grace with the watcher's final fetch (a fresh
# heartbeat already skips it; this covers the died-just-before case)
COT_HUERFANO_S = 90

_ULTIMA = [0.0]  # internal throttle: the cadence is set by the knob, not the loop


def auditar(cfg, ahora=None, pids_run_turn=None, es_run_turn=None):
    """One pass over the invariants. Runs in the poller's supervisor with
    its own throttle (auditor_cada_s; 0 = off). Each invariant is
    best-effort: if one blows up, the others still run — and NONE of
    them takes down the supervisor (same contract as on_* in hooks)."""
    ahora = time.time() if ahora is None else ahora
    cada = float(cfg.get("auditor_cada_s") or 0)
    if cada <= 0:
        return
    if ahora - _ULTIMA[0] < cada:
        return
    _ULTIMA[0] = ahora
    for chequeo in (_correlacion_fallos, _markers_huerfanos,
                    _workers_fantasma, _cola_dormida, _caches_huerfanos):
        try:
            chequeo(cfg, ahora, pids_run_turn=pids_run_turn,
                    es_run_turn=es_run_turn)
        except Exception:  # noqa: BLE001 — the auditor never breaks the poller
            pass


def _alertar(cfg, clave, texto, ahora):
    """Anti-spam: one alert per key every `auditor_alerta_cooldown_min`.
    The mark is left in the audit log (tipo=alerta, in the clave field)."""
    cooldown = float(cfg.get("auditor_alerta_cooldown_min") or 60) * 60
    for ev in audit.leer():
        if ev.get("tipo") == "alerta" and ev.get("clave") == clave \
                and ahora - float(ev.get("ts") or 0) < cooldown:
            return
    audit.registrar(None, "alerta", detalle=texto, extra={"clave": clave})
    uhttp.send(cfg, texto, loud=False)


# ------------------------------------------------------- invariants

def _correlacion_fallos(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """≥ N failures of the same (sid, tipo) within the window → "sick
    session". The sid=None bucket groups global failures (sends): the
    429/deadlock incident at 15:47 would have alerted here."""
    minimo = int(cfg.get("auditor_fallos_min") or 2)
    grupos = {}
    for ev in audit.leer():
        if ev.get("tipo") == "alerta":
            continue  # our own alerts are not failures to correlate
        ts = float(ev.get("ts") or 0)
        if ts < ahora - VENTANA_FALLOS_S:
            continue
        grupos.setdefault((ev.get("sid"), ev.get("tipo")), []).append(ts)
    for (sid, tipo), tss in grupos.items():
        if len(tss) < minimo:
            continue
        mins = max(1, int((ahora - min(tss)) / 60))
        if sid:
            clave = "fallos|%s|%s" % (sid, tipo)
            texto = ("⚠️ sess_%s: %d fallos de «%s» en %d min — sesión "
                     "enferma. Sugerencia: /task a otra conversación o "
                     "esperar la rotación." % (sid[-8:], len(tss), tipo, mins))
        else:
            clave = "fallos|global|%s" % tipo
            texto = ("⚠️ %d fallos de «%s» en %d min en los envíos — revisá "
                     "conectividad o /ping." % (len(tss), tipo, mins))
        _alertar(cfg, clave, texto, ahora)


def _markers_huerfanos(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """/stop marker with no owner → consume it (silent healing). The
    owner is the sid's worker: alive in the registry OR an active turn.
    The 60s threshold covers the race with a spawn in flight. The
    filename is the sanitized sid, and real sids contain no characters
    the sanitizer touches → filename == sid."""
    d = config.stop_dir()
    try:
        nombres = os.listdir(d)
    except OSError:
        return
    activos = running_turns()
    for nombre in nombres:
        ruta = os.path.join(d, nombre)
        try:
            if ahora - os.path.getmtime(ruta) <= MARKER_HUERFANO_S:
                continue  # fresh: a spawn may still be in flight
        except OSError:
            continue
        if nombre in activos:
            continue
        if zc_workers._workers().get(nombre) is not None:
            continue  # live worker: legitimate owner
        _stop_consume(nombre)
        audit.registrar(nombre, "marker-huerfano",
                        "consumido sin dueño (registry vacío, sin turno)")


def _workers_fantasma(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """run-turn ALIVE with no registry entry → recommend /stop go. With
    an empty registry, /stop, /status and the stuck-watch's F15 guard go
    blind (incident 15:40) — this makes it visible."""
    pids_run_turn = pids_run_turn or (lambda: zc_nuclear._nuc_pids(
        ["-f", "zcode-telegram.py --run-turn"]))
    es_run_turn = es_run_turn or zc_workers._run_turn_ok
    registrados = set()
    for _sid, w in zc_workers._workers().items():
        try:
            registrados.add(int(w.get("pid") or 0))
        except (TypeError, ValueError):
            continue
    fantasmas = [p for p in pids_run_turn()
                 if p != os.getpid() and p not in registrados
                 and es_run_turn(p)]
    if not fantasmas:
        return
    texto = ("🧟 Worker sin registro (pid %s): /stop, /status y el stuck-watch "
             "no lo ven — /stop go para rematarlo o /nuclear si persiste."
             % ", ".join(str(p) for p in fantasmas))
    _alertar(cfg, "fantasma|%s" % ",".join(str(p) for p in fantasmas),
             texto, ahora)


def _cola_dormida(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """Queued with no worker and no run in flight for > 10 min → nobody
    will pick it up (F17's "Mac busy" case has a run in flight and does
    NOT alert here). Legacy items without ts do not alert (no honest
    data, no notice)."""
    q = _store(config.ARCH_COLA).read()
    if not isinstance(q, dict):
        return
    activos = running_turns()
    for sid, items in q.items():
        if not isinstance(items, list) or not items:
            continue
        if zc_workers._workers().get(sid) is not None:
            continue  # live worker: it will pick the queue up when it finishes
        if sid in activos:
            continue  # Mac busy: F17, normal behavior
        tss = [float(i.get("ts") or 0) for i in items
               if isinstance(i, dict) and i.get("ts")]
        if not tss:
            continue  # legacy without ts
        mins = (ahora - min(tss)) / 60.0
        if mins < COLA_DORMIDA_MIN:
            continue
        titulo = ((items[0].get("act") or {}).get("title") or sid[-8:]) \
            if isinstance(items[0], dict) else sid[-8:]
        texto = ("📬 %d queued message%s for «%s» have gone %d min with no "
                 "run picking them up — /qgo to launch them or /task to the "
                 "conversation." % (len(items),
                                    "s" if len(items) != 1 else "",
                                    esc(str(titulo)), int(mins)))
        _alertar(cfg, "cola|%s" % sid, texto, ahora)


def _caches_huerfanos(cfg, ahora, pids_run_turn=None, es_run_turn=None):
    """F51a — CoT cache with vivo=True of a turn that FINISHED with no
    watcher (heartbeat expired) and no worker: the watcher died midway
    (kill -9/OOM) and nobody closed the cache. Idempotent healing (the
    house philosophy: close_cache keeps cajas/rótulos/ph/phs — the
    taps keep working —, it only clears vivo and records the reason).
    Without this the JSON would claim vivo forever."""
    from zcode_bot import razonamiento as zc_rz  # late: workers imports it
    try:
        nombres = os.listdir(config.STATE_DIR)
    except OSError:
        return
    activos = running_turns()
    for nombre in nombres:
        if not (nombre.startswith("cot-") and nombre.endswith(".json")):
            continue
        base = nombre[4:-5]
        sid, _, t0s = base.rpartition("-")
        if not sid or not t0s.isdigit():
            continue
        ruta = os.path.join(config.STATE_DIR, nombre)
        try:
            if ahora - os.path.getmtime(ruta) <= COT_HUERFANO_S:
                continue  # fresh: the live one may be in its final fetch
            with open(ruta) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not d.get("vivo") \
                or d.get("finalizado"):
            continue  # already closed or never opened
        if sid in activos:
            continue  # the turn is still running: closing it would silence the fallback
        if zc_workers._workers().get(sid) is not None:
            continue  # owning worker: its orphans_check closes it (F47d)
        if zc_rz.watcher_alive(sid):
            continue  # heartbeating: legitimate owner between writes
        if zc_rz.close_cache(sid, int(t0s), error="vigilante murió"):
            audit.registrar(sid, "cot-huerfano",
                            "cache vivo sin vigilante ni turno — cerrado")
