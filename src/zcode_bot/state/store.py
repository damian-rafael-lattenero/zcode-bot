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


class AtomicStore:
    """Archivo JSON de estado con escritura atómica y lock por thread.

    Formatos 100% compatibles con los archivos actuales del bot
    (bot-session.json, bot-queue.json, outbox.json, etc.).
    """

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

    def update_cross_process(self, fn, default):
        """update() + file-lock para multi-proceso (hooks vs poller)."""
        lock_path = self.path + ".lock"
        with self._lock:
            with open(lock_path, "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    return self.update(fn, default)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)