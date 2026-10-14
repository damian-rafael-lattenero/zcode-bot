"""fswatch — espera REACTIVA de cambios en archivos/directorios (F16).

Patrón: event-driven I/O multiplexing (kqueue EVFILT_VNODE, stdlib select
en macOS/BSD). kevent() BLOQUEA en el kernel sin consumir CPU: el thread
despierta exactamente cuando hay un cambio — nada de sondeos periódicos
ni busy-wait. Fallback portable (Linux de CI, plataformas sin kqueue):
sondeo con sleep — el thread TAMBIÉN duerme entre chequeos, misma
interfaz, peor latencia.

Notas kqueue (verificadas contra la API de Darwin):
- VNODE sobre un DIRECTORIO dispara con create/delete/rename DENTRO, no
  con appends a archivos existentes → para 'apareció a-<rid>.json' basta
  vigilar el dir; para 'creció el log' hay que vigilar el ARCHIVO.
- KQ_EV_CLEAR: el evento se re-arma solo.
- Carrera de armado: un cambio ENTRE el snapshot baseline y el registro
  del kqueue no dispara kevent ninguno. Lo cierra el ESCANEO DE
  SEGURIDAD: tras cada slice de control sin eventos se compara el
  estado actual contra el baseline (_snapshot) — el cambio temprano se
  rescata en ≤1 slice en vez de perderse hasta el timeout.
- Cada llamada arma su propio kqueue y lo cierra: sin estado compartido.
"""
import os
import select
import time


def kqueue_available():
    return hasattr(select, "kqueue")


def _snapshot(paths):
    """Huella barata del estado de paths (stat + listdir): la usa el
    escaneo de seguridad del kqueue y el fallback de sondeo. Un path que
    no existe hoy entra como None — cuando aparezca, la huella cambia."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            if os.path.isdir(p):
                out.append((st.st_mtime_ns, st.st_ino,
                            tuple(sorted(os.listdir(p)))))
            else:
                out.append((st.st_mtime_ns, st.st_ino, st.st_size))
        except OSError:
            out.append(None)
    return out


def wait_change(paths, timeout, poll=0.5, debounce=0.15):
    """Bloquea hasta que cambie algo en paths (dirs o archivos) o venza
    el timeout. True si hubo cambio. Con kqueue: reactivo (~0 latencia,
    cero CPU en la espera, inmune a la carrera de armado). Sin kqueue:
    sondea cada `poll` s (sleep entre chequeos — el thread no quema CPU)."""
    paths = [p for p in paths if p]
    if not paths:
        time.sleep(min(timeout, poll))
        return False
    if kqueue_available():
        return _wait_kqueue(paths, timeout, debounce, poll)
    return _wait_poll(paths, timeout, poll)


def wait_for(pred, paths, timeout, poll=2.0):
    """La espera canónica REACTIVA+TTL por una condición sobre archivos:
    chequea pred() y, si no da, se duerme en el kernel (kqueue) hasta que
    paths cambien — cero sondeo con sleeps. El slice ≤poll existe por si
    un append no dispara el vnode del archivo. True si pred dio True
    antes del TTL. Es el recv() con condición: quien espera NO gira."""
    deadline = time.time() + timeout
    while not pred():
        rest = deadline - time.time()
        if rest <= 0:
            return False
        wait_change(paths, timeout=min(rest, poll))
    return True


def _wait_kqueue(paths, timeout, debounce, poll=0.5):
    fds = []
    kq = select.kqueue()
    try:
        base = _snapshot(paths)  # ANTES de armar: nada anterior se pierde
        targets = []
        for p in paths:
            try:
                fd = os.open(p, os.O_RDONLY)  # dir o archivo
            except OSError:
                continue  # p.ej. el log de hoy aún no existe
            fds.append(fd)
            targets.append(select.kevent(
                fd, select.KQ_FILTER_VNODE,
                select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND))
        if not targets:
            # nada vigilable (todos los paths ausentes): sondeo — si un
            # path APARECE, su stat pasa de None a datos y se detecta
            return _wait_poll(paths, timeout, poll)
        deadline = time.time() + timeout
        while True:
            rest = deadline - time.time()
            if rest <= 0:
                return _snapshot(paths) != base  # último rescate
            # slice corto: el evento despierta al instante; sin eventos,
            # el escaneo de seguridad rescata lo anterior al armado
            events = kq.control(targets, 1, min(rest, max(poll, 0.05)))
            if events:
                time.sleep(debounce)  # coalescer ráfagas de writes
                return True
            if _snapshot(paths) != base:
                return True
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        kq.close()


def _wait_poll(paths, timeout, poll):
    base = _snapshot(paths)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        if _snapshot(paths) != base:
            return True
    return False
