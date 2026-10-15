"""workers.nuclear — Nuclear 2.0: emergencia completa con verificación.

1 pre-vuelo anuncia N corridas / M turnos Mac / K cola conservada
2 cerradura: spawn/drain/resume bloqueados durante el ciclo
3 frenado SIN limpiar la cola (semántica nueva)
4 heartbeat: el flag vivo no se purga por viejo; sin heartbeat, sí
5 watchdog: reporte final único o aviso de fracaso; flag siempre limpio
"""
import os
import json
import signal
import subprocess
import sys
import time

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview, fmt_tok
from zcode_bot.utils.logging import pwlog

from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles


LAUNCH_LABEL = "com.zcode.telegram-cmds"


def _nuc_pids(pgrep_args, run=None):
    run = run or subprocess.run
    r = run(["pgrep"] + list(pgrep_args), capture_output=True, text=True)
    return [int(x) for x in (r.stdout or "").split()]


def _nuc_wait(fn_bool, timeout, sleep=None, now=None):
    sleep = sleep or time.sleep
    now = now or time.time
    deadline = now() + timeout
    while now() < deadline:
        if fn_bool():
            return True
        sleep(0.5)
    return fn_bool()


def _nuc_kill_own_runtimes(run=None):
    run = run or subprocess.run
    import signal as _signal
    pids = []
    for _sid, w in WORKERS.items():
        pid = w.get("pid")
        if pid and pid != os.getpid():
            pids.append(int(pid))
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass
    if pids:
        time.sleep(5)

        def vivo(p):
            try:
                os.kill(p, 0)
                return True
            except OSError:
                return False
        for pid in pids:
            if vivo(pid):
                try:
                    os.kill(pid, _signal.SIGKILL)
                except OSError:
                    pass


def app_restart_cycle(run=None, pids=None, sleep=None, now=None):
    run = run or subprocess.run
    pids = pids or _nuc_pids
    sleep = sleep or time.sleep
    now = now or time.time

    def app_up():
        return bool(pids(["-x", "ZCode"]))

    def host_up():
        return bool(pids(["-f", "zcode-host-local"]))

    def orphans():
        return pids(["-P", "1", "zcode-cli"])

    rep = {"had_app": app_up(), "app_ok": False, "notes": []}
    if rep["had_app"]:
        run(["killall", "-TERM", "ZCode"], capture_output=True)
        if not _nuc_wait(lambda: not app_up(), 20, sleep, now):
            run(["killall", "-KILL", "ZCode"], capture_output=True)
            rep["notes"].append("la app no cerró con SIGTERM (SIGKILL)")
            _nuc_wait(lambda: not app_up(), 5, sleep, now)
    if host_up():
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
    _nuc_wait(lambda: not (app_up() or host_up() or orphans()), 5, sleep, now)
    run(["open", "-a", "ZCode"], capture_output=True)
    rep["app_ok"] = _nuc_wait(app_up, 40, sleep, now)
    return rep


def _nuc_pids(pgrep_args, run=None):
    run = run or subprocess.run
    r = run(["pgrep"] + list(pgrep_args), capture_output=True, text=True)
    return [int(x) for x in (r.stdout or "").split()]


def _nuc_wait(fn_bool, timeout, sleep=None, now=None):
    sleep = sleep or time.sleep
    now = now or time.time
    deadline = now() + timeout
    while now() < deadline:
        if fn_bool():
            return True
        sleep(0.5)
    return fn_bool()


def _nuc_kill_own_runtimes(run=None):
    run = run or subprocess.run
    import signal as _signal
    pids = []
    for _sid, w in WORKERS.items():
        pid = w.get("pid")
        if pid and pid != os.getpid():
            pids.append(int(pid))
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass
    if pids:
        time.sleep(5)

        def vivo(p):
            try:
                os.kill(p, 0)
                return True
            except OSError:
                return False
        for pid in pids:
            if vivo(pid):
                try:
                    os.kill(pid, _signal.SIGKILL)
                except OSError:
                    pass


def app_restart_cycle(run=None, pids=None, sleep=None, now=None):
    run = run or subprocess.run
    pids = pids or _nuc_pids
    sleep = sleep or time.sleep
    now = now or time.time

    def app_up():
        return bool(pids(["-x", "ZCode"]))

    def host_up():
        return bool(pids(["-f", "zcode-host-local"]))

    def orphans():
        return pids(["-P", "1", "zcode-cli"])

    rep = {"had_app": app_up(), "app_ok": False, "notes": []}
    if rep["had_app"]:
        run(["killall", "-TERM", "ZCode"], capture_output=True)
        if not _nuc_wait(lambda: not app_up(), 20, sleep, now):
            run(["killall", "-KILL", "ZCode"], capture_output=True)
            rep["notes"].append("la app no cerró con SIGTERM (SIGKILL)")
            _nuc_wait(lambda: not app_up(), 5, sleep, now)
    if host_up():
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
    _nuc_wait(lambda: not (app_up() or host_up() or orphans()), 5, sleep, now)
    run(["open", "-a", "ZCode"], capture_output=True)
    rep["app_ok"] = _nuc_wait(app_up, 40, sleep, now)
    return rep


_NUCLEAR_TTL = 120


def _nuclear_flag_path():
    return os.path.join(STATE_DIR, "bot-nuclear")


def _nuclear_heartbeat():
    p = _nuclear_flag_path()
    try:
        with open(p, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _nuclear_active():
    p = _nuclear_flag_path()
    try:
        if time.time() - os.path.getmtime(p) > _NUCLEAR_TTL:
            os.remove(p)
            pwlog("bot-nuclear huérfano purgado (TTL)")
            return False
        return True
    except OSError:
        return False


def _nuclear_snapshot():
    workers = list(WORKERS.items())
    mac = {sid: ts for sid, ts in running_turns().items()
           if WORKERS.get(sid) is None}
    cola = sum(len(_queue_peek(sid)) for sid in _queue_sids_with_items())
    return workers, mac, cola


def _nuclear_report_path():
    return os.path.join(STATE_DIR, "bot-nuclear-report.json")


def _nuclear_body(cfg):
    t0 = time.time()
    pwlog("NUCLEAR2 inicio")
    _nuclear_heartbeat()
    workers, mac, cola = _nuclear_snapshot()
    send(cfg, "☢️ <b>Nuclear</b> — pre-vuelo:\n"
              "⏹️ %d corrida%s del celular van a frenarse\n"
              "🖥️ %d turno%s de la Mac en vuelo (se pierden con la app)\n"
              "📬 %d mensaje%s en cola (SE CONSERVAN — corren al volver)"
          % (len(workers), "s" if len(workers) != 1 else "",
             len(mac), "s" if len(mac) != 1 else "",
             cola, "s" if cola != 1 else ""), loud=True)
    _nuclear_heartbeat()
    send(cfg, "☢️ Frenando corridas del bot…", loud=False)
    frenadas = 0
    for sid, w in workers:
        tit = WORKERS.flag_stop(sid)
        if tit is not None:
            _stop_request(sid)
            frenadas += 1
            pwlog("NUCLEAR2 stop", sid[-8:], (tit or "")[:40])
    _nuc_kill_own_runtimes()
    _nuclear_heartbeat()
    send(cfg, "☢️ Cerrando la app ZCode de la Mac (los %d turnos de la UI "
          "se pierden ahí)…" % len(mac), loud=False)
    rep = app_restart_cycle()
    _nuclear_heartbeat()
    dur = int(time.time() - t0)
    reporte = {"old_pid": os.getpid(), "t0": t0, "dur": dur,
               "frenadas": frenadas, "totales": len(workers),
               "mac": len(mac), "cola": cola, "vivos": vivos,
               "app_ok": bool(rep.get("app_ok")),
               "notes": rep.get("notes", [])}
    try:
        _store("bot-nuclear-report.json").write(reporte)
    except OSError:
        pass
    _spawn_nuclear_watchdog()
    pwlog("NUCLEAR2 kickstart (frenadas %d/%d, vivos %d)"
          % (frenadas, len(workers), vivos))
    time.sleep(2)
    try:
        subprocess.run(["launchctl", "kickstart", "-k",
                        "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)],
                       capture_output=True, timeout=15)
    except Exception as exc:
        log("errors", "nuclear kickstart: %s" % exc)


def _spawn_nuclear_watchdog():
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        err = open(os.path.join(LOG_DIR, "worker-stderr.log"), "a")
        subprocess.Popen([sys.executable, me, "--nuclear-watchdog"],
                         stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                         start_new_session=True, cwd=LOG_DIR)
        pwlog("NUCLEAR2 watchdog spawneado")
    except Exception as exc:
        log("errors", "nuclear watchdog spawn: %s" % exc)


def cmd_nuclear_watchdog():
    cfg = load_config()
    rep = _store("bot-nuclear-report.json").read() or {}
    old_pid = rep.get("old_pid") or 0

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
    dur = int(time.time() - (rep.get("t0") or time.time()))
    if nuevo is not None:
        time.sleep(3)
        notes = (" · " + "; ".join(rep.get("notes", []))
                 if rep.get("notes") else "")
        send(cfg, ("☢️ <b>Nuclear completo y verificado</b> (%ds)\n"
                   "⏹️ %s/%s corridas del celular frenadas\n"
                   "🖥️ App: %s · 🖥️ %d turno%s de la Mac perdidos\n"
                   "📬 Cola conservada: %d mensaje%s (corren al volver)\n"
                   "🔁 Poller nuevo: pid %d ✓%s"
                   % (dur, rep.get("frenadas", "?"), rep.get("totales", "?"),
                      "arriba ✓" if rep.get("app_ok") else "NO volvió ⚠️",
                      rep.get("mac", 0), "s" if rep.get("mac") != 1 else "",
                      rep.get("cola", 0),
                      "s" if rep.get("cola") != 1 else "",
                      nuevo, esc(notes))),
              loud=True)
        pwlog("NUCLEAR2 verificado: poller nuevo", nuevo)
    else:
        send(cfg, "⛔️ <b>El poller NO volvió tras el nuclear</b> (90 s) — "
                  "revisá launchctl o abrí la app. Cuando vuelva: /ping. "
                  "La cola y los markers quedaron intactos.", loud=True)
        pwlog("NUCLEAR2 FALLÓ: poller no volvió")
    for p in (_nuclear_flag_path(), _nuclear_report_path()):
        try:
            os.remove(p)
        except OSError:
            pass


import os
import json
import signal
import sys
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview, fmt_tok, fmt_num
from zcode_bot.utils.logging import pwlog

from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles