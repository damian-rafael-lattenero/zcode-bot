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
- KQ_EV_CLEAR: el evento se re-arma solo; el loop limita cada control a
  5 s para respetar el deadline del caller.
- Cada llamada arma su propio kqueue y lo cierra: sin estado compartido.
"""
import os
import select
import time


def kqueue_available():
    return hasattr(select, "kqueue")


def wait_change(paths, timeout, poll=0.5, debounce=0.15):
    """Bloquea hasta que cambie algo en paths (dirs o archivos) o venza
    el timeout. True si hubo cambio. Con kqueue: reactivo (~0 latencia,
    cero CPU en la espera). Sin kqueue: sondea cada `poll` s (sleep entre
    chequeos — el thread no quema CPU)."""
    paths = [p for p in paths if p]
    if not paths:
        time.sleep(min(timeout, poll))
        return False
    if kqueue_available():
        return _wait_kqueue(paths, timeout, debounce)
    return _wait_poll(paths, timeout, poll)


def _wait_kqueue(paths, timeout, debounce):
    fds = []
    kq = select.kqueue()
    try:
        targets = []
        for p in paths:
            try:
                fd = os.open(p, os.O_RDONLY)  # dir o archivo
            except OSError:
                continue  # p.ej. el log de hoy aún no existe: cubre el dir
            fds.append(fd)
            targets.append(select.kevent(
                fd, select.KQ_FILTER_VNODE,
                select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND))
        if not targets:
            time.sleep(min(timeout, 0.5))
            return False
        deadline = time.time() + timeout
        while True:
            rest = deadline - time.time()
            if rest <= 0:
                return False
            events = kq.control(targets, 1, min(rest, 5.0))
            if events:
                time.sleep(debounce)  # coalescer ráfagas de writes
                return True
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        kq.close()


def _wait_poll(paths, timeout, poll):
    def snap():
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

    base = snap()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        if snap() != base:
            return True
    return False
