"""nuclear — ☢️ Nuclear 2.0: reinicio total anunciado y verificado (F4e).

La operación de emergencia del bot: frena las corridas del celular,
reinicia la app ZCode de la Mac y verifica que el poller vuelva. Corre
en un thread del poller (body) + un watchdog detached que sobrevive al
kickstart. Transporte POR ATRIBUTO (uhttp.send).
"""
import os
import subprocess
import sys
import time

from zcode_bot import config
from zcode_bot.core import (_store, _workers, _stop_request, running_turns,
                            _queue_peek, _queue_sids_with_items)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log, pwlog

LAUNCH_LABEL = "com.zcode.telegram-cmds"
_NUCLEAR_TTL = 120  # sin heartbeat hace 120s = huérfano


def _nuc_pids(pgrep_args, run=None):
    """PIDs según pgrep con los args dados (inyectable para tests)."""
    run = run or subprocess.run
    r = run(["pgrep"] + list(pgrep_args), capture_output=True, text=True)
    return [int(x) for x in (r.stdout or "").split()]


def _nuc_wait(fn_bool, timeout, sleep=None, now=None):
    """Espera hasta que fn_bool() dé True o venza el timeout (0.5s por paso).
    now inyectable para tests (reloj falso, sin esperas reales)."""
    sleep = sleep or time.sleep
    now = now or time.time
    deadline = now() + timeout
    while now() < deadline:
        if fn_bool():
            return True
        sleep(0.5)
    return fn_bool()


def _nuc_kill_own_runtimes(run=None):
    """F16: SIGTERM a los WORKERS del registry (procesos independientes del
    poller — los runtimes ya no son hijos de acá, son nietos). Grace de 5s
    y SIGKILL al sobreviviente. Los workers también recibieron el marker
    de /stop (bot_stop arriba): esto es el remate duro."""
    run = run or subprocess.run
    import signal as _signal
    pids = []
    for _sid, w in _workers().items():
        pid = w.get("pid")
        if pid and pid != os.getpid():
            pids.append(int(pid))
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass
    if pids:
        # Espera reactiva: duerme en kernel hasta que los procesos mueran
        # (reemplaza el sleep(5) fijo por wait reactivo)
        def alguno_vivo():
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    return True
                except OSError:
                    pass
            return False
        _nuc_wait(lambda: not alguno_vivo(), timeout=5)
        # Segundo pase: SIGKILL a los que sobrevivieron
        for pid in pids:
            try:
                os.kill(pid, 0)
                os.kill(pid, _signal.SIGKILL)
            except OSError:
                pass


def app_restart_cycle(run=None, pids=None, sleep=None, now=None):
    """Ciclo completo de reinicio de la app ZCode (para /nuclear):
    SIGTERM (cierre normal, sin permisos de osascript) → espera de salida
    (SIGKILL de rescate si no cierra) → host huérfano solo si la app YA
    murió (única excepción a la regla, ADR-006) → cli huérfanos (ppid 1)
    rematados → recién con el árbol COMPLETO muerto se reabre con open -a.
    Sin sobrevivientes no quedan registraciones que el Dock muestre como
    íconos "exec". Devuelve {had_app, app_ok, notes}. Inyectable p/ tests."""
    run = run or subprocess.run
    pids = pids or _nuc_pids
    sleep = sleep or time.sleep
    now = now or time.time

    def app_up():
        return bool(pids(["-x", "ZCode"]))

    def host_up():
        return bool(pids(["-f", "zcode-host-local"]))

    def orphans():
        return pids(["-P", "1", "zcode-cli"])  # cli reparentados a launchd

    rep = {"had_app": app_up(), "app_ok": False, "notes": []}
    if rep["had_app"]:
        run(["killall", "-TERM", "ZCode"], capture_output=True)
        if not _nuc_wait(lambda: not app_up(), 20, sleep, now):
            run(["killall", "-KILL", "ZCode"], capture_output=True)
            rep["notes"].append("la app no cerró con SIGTERM (SIGKILL)")
            _nuc_wait(lambda: not app_up(), 5, sleep, now)
    if host_up():  # muere con la app; si queda colgado:
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
    # árbol completo muerto ANTES de reabrir: estado limpio para el Dock
    _nuc_wait(lambda: not (app_up() or host_up() or orphans()), 5, sleep, now)
    run(["open", "-a", "ZCode"], capture_output=True)
    rep["app_ok"] = _nuc_wait(app_up, 40, sleep, now)
    return rep


def _nuclear_flag_path():
    return os.path.join(config.STATE_DIR, "bot-nuclear")


def _nuclear_heartbeat():
    """Nuclear 2.0: refresca el flag en CADA fase del ciclo. El TTL ya no
    es 'edad del archivo' sino 'tiempo sin heartbeat' — un nuclear real de
    cualquier duración no se purga a mitad (con app rebelde puede tardar
    ~95s+). El huérfano del incidente 18:56 sigue auto-sanando igual:
    cuando el proceso muere, nadie hace touch."""
    p = _nuclear_flag_path()
    try:
        with open(p, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _nuclear_active():
    """F16: en ARCHIVO (el flag in-memory moriría con el poller que el
    propio nuclear kickstea al final). F21: TTL contra huérfanos (incidente
    18:56: bloqueó TODOS los deploys en silencio hasta 22:33). Nuclear 2.0:
    el ciclo refresca el flag por heartbeat, así que el TTL mide inactividad."""
    p = _nuclear_flag_path()
    try:
        if time.time() - os.path.getmtime(p) > _NUCLEAR_TTL:
            os.remove(p)  # auto-sanación: el gate queda desbloqueado
            pwlog("bot-nuclear huérfano purgado (TTL)")
            return False
        return True
    except OSError:
        return False


def _nuclear_run(cfg):
    """☢️ Nuclear 2.0: operación anunciada y verificada. Corre en un
    thread del poller. El flag lo escribe acá (heartbeat en cada fase);
    el finally es solo rescate — la limpieza NORMAL la hace el watchdog
    post-reinicio, que sobrevive al kickstart."""
    _nuclear_heartbeat()
    try:
        _nuclear_body(cfg)
    finally:
        # rescate: si el body murió ANTES de spawnear el watchdog, el flag
        # no puede quedar bloqueando deploys (TTL lo purgaría, pero esto
        # es inmediato). Si el watchdog ya corre, este remove es inofensivo:
        # el watchdog guarda su propia copia del reporte.
        try:
            os.remove(_nuclear_flag_path())
        except OSError:
            pass


def _nuclear_snapshot():
    """Pre-vuelo: qué hay vivo ahora mismo (para anunciar lo que se pierde)."""
    workers = list(_workers().items())
    mac = {sid: ts for sid, ts in running_turns().items()
           if _workers().get(sid) is None}
    cola = sum(len(_queue_peek(sid)) for sid in _queue_sids_with_items())
    return workers, mac, cola


def _nuclear_report_path():
    return os.path.join(config.STATE_DIR, "bot-nuclear-report.json")


def _nuclear_body(cfg):
    t0 = time.time()
    pwlog("NUCLEAR2 inicio")
    # Fase 0 — pre-vuelo: anuncio honesto de lo que va a pasar
    _nuclear_heartbeat()
    workers, mac, cola = _nuclear_snapshot()
    uhttp.send(cfg, "☢️ <b>Nuclear</b> — pre-vuelo:\n"
               "⏹️ %d corrida%s del celular van a frenarse\n"
               "🖥️ %d turno%s de la Mac en vuelo (se pierden con la app)\n"
               "📬 %d mensaje%s en cola (SE CONSERVAN — corren al volver)"
               % (len(workers), "s" if len(workers) != 1 else "",
                  len(mac), "s" if len(mac) != 1 else "",
                  cola, "s" if cola != 1 else ""), loud=True)
    # Fase 1+2 — cerradura activa (los guards leen _nuclear_active) + frenado
    _nuclear_heartbeat()
    uhttp.send(cfg, "☢️ Frenando corridas del bot…", loud=False)
    frenadas = 0
    for sid, w in workers:
        tit = _workers().flag_stop(sid)  # SIN _queue_take: la cola se conserva
        if tit is not None:
            _stop_request(sid)
            frenadas += 1
            pwlog("NUCLEAR2 stop", sid[-8:], (tit or "")[:40])
    _nuc_kill_own_runtimes()
    # verificación: el registry tiene que quedar vacío
    vivos = len(_workers().items())
    _nuclear_heartbeat()
    # Fase 3 — app (los turnos de la UI mueren con ella, ADR-006)
    uhttp.send(cfg, "☢️ Cerrando la app ZCode de la Mac (los %d turnos de la "
               "UI se pierden ahí)…" % len(mac), loud=False)
    rep = app_restart_cycle()
    _nuclear_heartbeat()
    # Fase 4 — watchdog post-reinicio: sobrevive al kickstart y cierra la
    # operación con veredicto (hoy el fracaso del arranque era silencioso)
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
    # kickstart: mata ESTE poller — nada de este hilo corre después
    pwlog("NUCLEAR2 kickstart (frenadas %d/%d, vivos %d)"
          % (frenadas, len(workers), vivos))
    time.sleep(2)
    try:
        subprocess.run(["launchctl", "kickstart", "-k",
                        "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)],
                       capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log("errors", "nuclear kickstart: %s" % exc)


def _spawn_nuclear_watchdog():
    """Proceso detached que verifica el reinicio y manda el reporte final.
    Debe sobrevivir al kickstart que mata a su padre (patrón F16)."""
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        err = open(os.path.join(config.LOG_DIR, "worker-stderr.log"), "a")
        subprocess.Popen([sys.executable, me, "--nuclear-watchdog"],
                         stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                         start_new_session=True, cwd=config.LOG_DIR)
        pwlog("NUCLEAR2 watchdog spawneado")
    except Exception as exc:  # noqa: BLE001 — sin watchdog, el TTL rescata
        log("errors", "nuclear watchdog spawn: %s" % exc)


def cmd_nuclear_watchdog():
    """☢️ Nuclear 2.0 — cierre verificado. Corre DETACHED (sobrevive al
    kickstart que mata al poller). Espera hasta 90s al poller nuevo:
    ✅ apareció → reporte final único; ⛔️ no apareció → aviso de fracaso.
    En AMBOS casos limpia el flag (nunca queda bloqueando deploys)."""
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
        time.sleep(3)  # que termine de arrancar antes del veredicto
        notes = (" · " + "; ".join(rep.get("notes", []))
                 if rep.get("notes") else "")
        uhttp.send(cfg, ("☢️ <b>Nuclear completo y verificado</b> (%ds)\n"
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
        uhttp.send(cfg, "⛔️ <b>El poller NO volvió tras el nuclear</b> (90 s) — "
                   "revisá launchctl o abrí la app. Cuando vuelva: /ping. "
                   "La cola y los markers quedaron intactos.", loud=True)
        pwlog("NUCLEAR2 FALLÓ: poller no volvió")
    # limpieza SIEMPRE: el flag jamás bloquea deploys después de acá
    for p in (_nuclear_flag_path(), _nuclear_report_path()):
        try:
            os.remove(p)
        except OSError:
            pass
