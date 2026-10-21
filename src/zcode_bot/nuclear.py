"""nuclear — ☢️ Nuclear 2.0: announced and verified full restart (F4e).

The bot's emergency operation: stops the phone runs, restarts the Mac's
ZCode app and verifies the poller comes back. Runs in a poller thread
(body) + a detached watchdog that survives the kickstart. Transport
BY ATTRIBUTE (uhttp.send).
"""
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from zcode_bot import config
from zcode_bot.core import (_store, _workers, _stop_request, running_turns,
                            _queue_peek, _queue_sids_with_items)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log, pwlog

LAUNCH_LABEL = "com.zcode.telegram-cmds"
_NUCLEAR_TTL = 120  # no heartbeat for 120s = orphan


@dataclass
class NuclearReport:
    """Cycle report (state/bot-nuclear-report.json): written by the
    body BEFORE the kickstart (which kills the poller) and read by the
    detached watchdog — it is the only contract between the two
    processes."""
    old_pid: int = 0
    t0: float = 0.0
    dur: int = 0
    frenadas: int = 0
    totales: int = 0
    mac: int = 0
    cola: int = 0
    vivos: int = 0
    app_ok: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"old_pid": self.old_pid, "t0": self.t0, "dur": self.dur,
                "frenadas": self.frenadas, "totales": self.totales,
                "mac": self.mac, "cola": self.cola, "vivos": self.vivos,
                "app_ok": bool(self.app_ok), "notes": list(self.notes)}

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        notes_raw = d.get("notes")
        return cls(
            old_pid=int(d.get("old_pid") or 0),
            t0=float(d.get("t0") or 0.0),
            dur=int(d.get("dur") or 0),
            frenadas=int(d.get("frenadas") or 0),
            totales=int(d.get("totales") or 0),
            mac=int(d.get("mac") or 0),
            cola=int(d.get("cola") or 0),
            vivos=int(d.get("vivos") or 0),
            app_ok=bool(d.get("app_ok")),
            notes=[str(n) for n in notes_raw]
            if isinstance(notes_raw, (list, tuple)) else [])


def _nuc_pids(pgrep_args, run=None):
    """PIDs per pgrep with the given args (injectable for tests)."""
    run = run or subprocess.run
    r = run(["pgrep"] + list(pgrep_args), capture_output=True, text=True)
    return [int(x) for x in (r.stdout or "").split()]


def _nuc_wait(fn_bool, timeout, sleep=None, now=None):
    """Waits until fn_bool() returns True or the timeout expires
    (0.5s per step). now is injectable for tests (fake clock, no real
    waits)."""
    sleep = sleep or time.sleep
    now = now or time.time
    deadline = now() + timeout
    while now() < deadline:
        if fn_bool():
            return True
        sleep(0.5)
    return fn_bool()


def _nuc_kill_own_runtimes(run=None, es_cot_vivo=None):
    """F16: SIGTERM to the registry's WORKERS (processes independent of
    the poller — the runtimes are no longer children of this process,
    they are grandchildren). 5s grace, then SIGKILL for any survivor.
    The workers already got the /stop marker too (bot_stop above):
    this is the hard kill-off.
    F44: HOT CoT watchers (pid-files cot-vivo-*.pid) go into the same
    kill-off — each one holds a read-only runtime open — verifying the
    cmdline (es_cot_vivo) before touching a pid that may have been
    recycled; the pid-files are then cleaned up."""
    run = run or subprocess.run
    import signal as _signal
    from zcode_bot import razonamiento as _rz
    if es_cot_vivo is None:
        from zcode_bot.workers import _cot_vivo_ok  # lazy: workers imports nuclear
        es_cot_vivo = _cot_vivo_ok
    pids = []
    for _sid, w in _workers().items():
        pid = w.get("pid")
        if pid and pid != os.getpid():
            pids.append(int(pid))
    for pid in _rz.vigilantes_pids():
        if pid != os.getpid() and pid not in pids and es_cot_vivo(pid):
            pids.append(pid)
    _rz.vigilantes_limpiar()
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass
    if pids:
        # Reactive wait: sleeps in the kernel until the processes die
        # (replaces the fixed sleep(5) with a reactive wait)
        def alguno_vivo():
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    return True
                except OSError:
                    pass
            return False
        _nuc_wait(lambda: not alguno_vivo(), timeout=5)
        # Second pass: SIGKILL the survivors
        for pid in pids:
            try:
                os.kill(pid, 0)
                os.kill(pid, _signal.SIGKILL)
            except OSError:
                pass


def app_restart_cycle(run=None, pids=None, sleep=None, now=None):
    """Full ZCode app restart cycle (for /nuclear):
    SIGTERM (normal close, no osascript permissions) → wait for exit
    (rescue SIGKILL if it does not close) → orphan host only if the app
    is ALREADY dead (the one exception to the rule, ADR-006) → orphan
    cli processes (ppid 1) killed off → only once the COMPLETE tree is
    dead is it reopened with open -a.
    With no survivors there are no registrations left for the Dock to
    show as "exec" icons. Returns {had_app, app_ok, notes}. Injectable
    for tests."""
    run = run or subprocess.run
    pids = pids or _nuc_pids
    sleep = sleep or time.sleep
    now = now or time.time

    def app_up():
        return bool(pids(["-x", "ZCode"]))

    def host_up():
        return bool(pids(["-f", "zcode-host-local"]))

    def orphans():
        return pids(["-P", "1", "zcode-cli"])  # cli reparented to launchd

    rep = {"had_app": app_up(), "app_ok": False, "notes": []}
    if rep["had_app"]:
        run(["killall", "-TERM", "ZCode"], capture_output=True)
        if not _nuc_wait(lambda: not app_up(), 20, sleep, now):
            run(["killall", "-KILL", "ZCode"], capture_output=True)
            rep["notes"].append("la app no cerró con SIGTERM (SIGKILL)")
            _nuc_wait(lambda: not app_up(), 5, sleep, now)
    if host_up():  # dies with the app; if it hangs around:
        if not _nuc_wait(lambda: not host_up(), 15, sleep, now):
            run(["pkill", "-TERM", "-f", "zcode-host-local"], capture_output=True)
            rep["notes"].append("host huérfano terminado a mano")
            _nuc_wait(lambda: not host_up(), 5, sleep, now)
    vivos = orphans()
    for pid in vivos:
        run(["kill", "-TERM", str(pid)], capture_output=True)
    if vivos and not _nuc_wait(lambda: not orphans(), 5, sleep, now):
        for pid in vivos:
            run(["kill", "-KILL", str(pid)], capture_output=True)
        rep["notes"].append("cli huérfanos rematados con SIGKILL")
        _nuc_wait(lambda: not orphans(), 3, sleep, now)
    # full tree dead BEFORE reopening: clean state for the Dock
    _nuc_wait(lambda: not (app_up() or host_up() or orphans()), 5, sleep, now)
    run(["open", "-a", "ZCode"], capture_output=True)
    rep["app_ok"] = _nuc_wait(app_up, 40, sleep, now)
    return rep


def _nuclear_flag_path():
    return os.path.join(config.STATE_DIR, "bot-nuclear")


def _nuclear_heartbeat():
    """Nuclear 2.0: refreshes the flag at EVERY phase of the cycle. The
    TTL is no longer 'file age' but 'time without a heartbeat' — a real
    nuclear of any duration is not purged halfway (with a rebellious app
    it can take ~95s+). The orphan from the 18:56 incident still
    self-heals the same way: when the process dies, nobody refreshes
    the flag."""
    p = _nuclear_flag_path()
    try:
        with open(p, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _nuclear_active():
    """F16: in a FILE (an in-memory flag would die with the poller that
    nuclear itself kickstarts at the end). F21: TTL against orphans
    (incident 18:56: it silently blocked ALL deploys until 22:33).
    Nuclear 2.0: the cycle refreshes the flag by heartbeat, so the TTL
    measures inactivity."""
    p = _nuclear_flag_path()
    try:
        if time.time() - os.path.getmtime(p) > _NUCLEAR_TTL:
            os.remove(p)  # self-healing: the gate is left unblocked
            pwlog("bot-nuclear orphan purged (TTL)")
            return False
        return True
    except OSError:
        return False


def _nuclear_run(cfg):
    """☢️ Nuclear 2.0: announced and verified operation. Runs in a
    poller thread. The flag is written here (heartbeat at every phase);
    the finally block is rescue only — the NORMAL cleanup is done by the
    post-restart watchdog, which survives the kickstart."""
    _nuclear_heartbeat()
    try:
        _nuclear_body(cfg)
    finally:
        # rescue: if the body died BEFORE spawning the watchdog, the flag
        # cannot be left blocking deploys (the TTL would purge it, but
        # this is immediate). If the watchdog is already running, this
        # remove is harmless: the watchdog keeps its own copy of the
        # report.
        try:
            os.remove(_nuclear_flag_path())
        except OSError:
            pass


def _nuclear_snapshot():
    """Pre-flight: what is alive right now (to announce what gets lost)."""
    workers = list(_workers().items())
    mac = {sid: ts for sid, ts in running_turns().items()
           if _workers().get(sid) is None}
    cola = sum(len(_queue_peek(sid)) for sid in _queue_sids_with_items())
    return workers, mac, cola


def _nuclear_report_path():
    return os.path.join(config.STATE_DIR, "bot-nuclear-report.json")


def _nuclear_body(cfg):
    t0 = time.time()
    pwlog("NUCLEAR2 start")
    # Phase 0 — pre-flight: honest announcement of what is about to happen
    _nuclear_heartbeat()
    workers, mac, cola = _nuclear_snapshot()
    uhttp.send(cfg, "☢️ <b>Nuclear</b> — pre-vuelo:\n"
               "⏹️ %d corrida%s del celular van a frenarse\n"
               "🖥️ %d turno%s de la Mac en vuelo (se pierden con la app)\n"
               "📬 %d mensaje%s en cola (SE CONSERVAN — corren al volver)"
               % (len(workers), "s" if len(workers) != 1 else "",
                  len(mac), "s" if len(mac) != 1 else "",
                  cola, "s" if cola != 1 else ""), loud=True)
    # Phases 1+2 — active lockdown (the guards read _nuclear_active) + stopping
    _nuclear_heartbeat()
    uhttp.send(cfg, "☢️ Frenando corridas del bot…", loud=False)
    frenadas = 0
    for sid, w in workers:
        tit = _workers().flag_stop(sid)  # NO _queue_take: the queue is preserved
        if tit is not None:
            _stop_request(sid)
            frenadas += 1
            pwlog("NUCLEAR2 stop", sid[-8:], (tit or "")[:40])
    _nuc_kill_own_runtimes()
    # verification: the registry must end up empty
    vivos = len(_workers().items())
    _nuclear_heartbeat()
    # Phase 3 — app (UI turns die with it, ADR-006)
    uhttp.send(cfg, "☢️ Cerrando la app ZCode de la Mac (los %d turnos de la "
               "UI se pierden ahí)…" % len(mac), loud=False)
    rep = app_restart_cycle()
    _nuclear_heartbeat()
    # Phase 4 — post-restart watchdog: survives the kickstart and closes
    # the operation with a verdict (until now a failed start was silent)
    dur = int(time.time() - t0)
    reporte = NuclearReport(old_pid=os.getpid(), t0=t0, dur=dur,
                            frenadas=frenadas, totales=len(workers),
                            mac=len(mac), cola=cola, vivos=vivos,
                            app_ok=bool(rep.get("app_ok")),
                            notes=rep.get("notes", []))
    try:
        _store("bot-nuclear-report.json").write(reporte.to_dict())
    except OSError:
        pass
    _spawn_nuclear_watchdog()
    # kickstart: kills THIS poller — nothing in this thread runs after it
    pwlog("NUCLEAR2 kickstart (stopped %d/%d, alive %d)"
          % (frenadas, len(workers), vivos))
    time.sleep(2)
    try:
        subprocess.run(["launchctl", "kickstart", "-k",
                        "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)],
                       capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log("errors", "nuclear kickstart: %s" % exc)


def _spawn_nuclear_watchdog():
    """Detached process that verifies the restart and sends the final
    report. It must survive the kickstart that kills its parent
    (F16 pattern)."""
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        err = open(os.path.join(config.LOG_DIR, "worker-stderr.log"), "a")
        subprocess.Popen([sys.executable, me, "--nuclear-watchdog"],
                         stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                         start_new_session=True, cwd=config.LOG_DIR)
        pwlog("NUCLEAR2 watchdog spawned")
    except Exception as exc:  # noqa: BLE001 — without a watchdog, the TTL rescues
        log("errors", "nuclear watchdog spawn: %s" % exc)


def cmd_nuclear_watchdog():
    """☢️ Nuclear 2.0 — verified close. Runs DETACHED (survives the
    kickstart that kills the poller). Waits up to 90s for the new poller:
    ✅ it came up → single final report; ⛔️ it did not → failure notice.
    In BOTH cases it clears the flag (never left blocking deploys)."""
    cfg = load_config()
    rep = NuclearReport.from_dict(_store("bot-nuclear-report.json").read())
    old_pid = rep.old_pid

    def poller_vivo():
        linea = subprocess.run(["pgrep", "-f",
                                "zcode-telegram.py watch-commands"],
                               capture_output=True, text=True).stdout
        for pid_txt in (linea or "").split():
            try:
                if int(pid_txt) != old_pid:
                    return int(pid_txt)
            except ValueError:
                continue
        return None

    nuevo = None
    deadline = time.time() + 90
    while time.time() < deadline and nuevo is None:
        nuevo = poller_vivo()
        if nuevo is None:
            time.sleep(2)
    dur = int(time.time() - (rep.t0 or time.time()))
    if nuevo is not None:
        time.sleep(3)  # let it finish starting before the verdict
        notes = (" · " + "; ".join(rep.notes) if rep.notes else "")
        uhttp.send(cfg, ("☢️ <b>Nuclear completo y verificado</b> (%ds)\n"
                         "⏹️ %s/%s corridas del celular frenadas\n"
                         "🖥️ App: %s · 🖥️ %d turno%s de la Mac perdidos\n"
                         "📬 Cola conservada: %d mensaje%s (corren al volver)\n"
                         "🔁 Poller nuevo: pid %d ✓%s"
                         % (dur, rep.frenadas, rep.totales,
                            "arriba ✓" if rep.app_ok else "NO volvió ⚠️",
                            rep.mac, "s" if rep.mac != 1 else "",
                            rep.cola,
                            "s" if rep.cola != 1 else "",
                            nuevo, esc(notes))),
                   loud=True)
        pwlog("NUCLEAR2 verified: new poller", nuevo)
    else:
        uhttp.send(cfg, "⛔️ <b>El poller NO volvió tras el nuclear</b> (90 s) — "
                   "revisá launchctl o abrí la app. Cuando vuelva: /ping. "
                   "La cola y los markers quedaron intactos.", loud=True)
        pwlog("NUCLEAR2 FAILED: poller did not come back")
    # cleanup ALWAYS: the flag never blocks deploys after this point
    for p in (_nuclear_flag_path(), _nuclear_report_path()):
        try:
            os.remove(p)
        except OSError:
            pass
