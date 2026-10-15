"""state.AtomicStore — patrón Atomic Write (tmp + os.replace + fsync).

Referencia: atomicidad de rename(2) en POSIX — práctica estándar para
estado persistente (la usan sqlite, systemd, etc.). Un crash a mitad de
escritura NUNCA deja un JSON cortado: queda el archivo anterior completo.

Para archivos tocados por VARIOS PROCESOS (hooks + poller, ej. outbox),
update() toma además un file-lock (fcntl.flock) sobre un .lock lateral:
read-modify-write sin perder escrituras concurrentes.

Regla interna (lección del deadlock 2026-10-12): el lock ES reentrante
(RLock) y los métodos públicos nunca anidan adquisiciones no reentrantes.
"""
import fcntl
import json
import os
import threading
import time


class AtomicStore:
    """Archivo JSON de estado con escritura atómica y lock por thread.

    Formatos 100% compatibles con los archivos actuales del bot
    (bot-session.json, bot-queue.json, outbox.json, etc.).
    """

    # F8 (incidente 15:47): el flock NO es reentrante entre fds — el RLock
    # sí lo es intra-proceso, pero anidar update_cross_process del MISMO
    # archivo desde el mismo thread abre un segundo fd cuyo flock(LOCK_EX)
    # bloquea para siempre contra el primero (auto-deadlock: el outbox
    # flush llamaba send dentro del lock y el send fallido re-encolaba con
    # OTRO flock → poller congelado 30+ min). Además del arreglo en
    # outbox_flush (sends FUERA del lock), el flock ahora tiene DEADLINE:
    # si no se consigue en tiempo, OSError con nombre de archivo — un
    # deadlock futuro degrada con log, nunca congela un proceso entero.
    FLOCK_TIMEOUT_S = 10.0

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()

    # ---------- lectura ----------

    def read(self, default=None):
        with self._lock:
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (OSError, ValueError):
                return default

    # ---------- escritura ----------

    def write(self, data):
        with self._lock:
            self._write_nolock(data)

    def _write_nolock(self, data):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ---------- read-modify-write ----------

    def update(self, fn, default):
        """Lee, aplica fn(data) -> nueva data, escribe. Atómico intra-proceso."""
        with self._lock:
            cur = self.read(default)
            nuevo = fn(cur if cur is not None else default)
            if nuevo is not None:
                self._write_nolock(nuevo)
            return nuevo

    def update_cross_process(self, fn, default, timeout=None):
        """update() + file-lock para multi-proceso (hooks vs poller).
        Espera el lock con deadline (no bloquea eterno: incidente 15/10 —
        un flock anidado congeló el poller 30 min)."""
        lock_path = self.path + ".lock"
        t0 = time.time()
        queda = self.FLOCK_TIMEOUT_S if timeout is None else timeout
        with self._lock:
            with open(lock_path, "a+") as lf:
                while True:
                    try:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.time() - t0 >= queda:
                            raise OSError(
                                "flock timeout (%.0fs): %s — lock retenido "
                                "por otro thread/proceso (posible anidamiento)"
                                % (queda, lock_path))
                        time.sleep(0.1)
                try:
                    return self.update(fn, default)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)