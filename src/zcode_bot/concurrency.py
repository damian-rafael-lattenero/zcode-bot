"""concurrency — WorkersRegistry (patrón Registry, file-backed) y
QuestionsBroker (correlación request/response por id).

Regla de oro (lección del deadlock del 2026-10-12): threading.Lock NO es
reentrante. Estos tipos exponen SOLO métodos que toman el lock una vez y
nunca llaman a otro método que lo tome (RLock como cinturón de seguridad).

F16: WorkersRegistry vive en state/bot-workers.json con flock — poller y
workers-proceso comparten la misma vista, y un restart del poller ya no
borra los turnos en vuelo. Entradas podadas por PID muerto (os.kill(pid,0))
con tope de edad anti PID-reuse. El campo `client` NUNCA se serializa:
es un mapa local del proceso (transición al /stop por marker).
"""
import os
import threading
import time

from zcode_bot import state as _state


def _pid_alive(pid):
    """True si el proceso existe. PermissionError = existe (otro usuario)."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True  # ambiguo: no podar (falso positivo tolerable)


class WorkersRegistry:
    """sid -> {t0, prompt, ws, titulo, stopped, pid} en bot-workers.json.

    Contrato de archivo (estable entre versiones — NO cambiar sin
    coordinar): claves por sid, cada entrada con pid (dueño), t0 (epoch),
    stopped (bool), prompt/ws/titulo (reporting). Escrituras SIEMPRE bajo
    flock (update_cross_process)."""

    MAX_EDAD_S = 4 * 3600  # anti PID-reuse: entrada vieja aunque "viva"

    def __init__(self, path):
        self._store = _state.AtomicStore(path)
        self._clients = {}  # sid -> ProtoClient (solo este proceso)
        self._lock = threading.RLock()

    # ---------- poda ----------

    def _prune(self):
        """Saca entradas de workers muertos (crash/restart). Devuelve el
        dict vivo. Sin cambios → no escribe (evita churn por hook)."""
        muertos = []

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            ahora = time.time()
            for k, ent in list(q.items()):
                if not isinstance(ent, dict):
                    muertos.append(k)
                    continue
                if ent.get("pid") == os.getpid():
                    continue  # trabajadores de ESTE proceso
                vieja = ahora - float(ent.get("t0") or 0) > self.MAX_EDAD_S
                if vieja or not _pid_alive(ent.get("pid")):
                    muertos.append(k)
            if not muertos:
                return None  # AtomicStore.update: None = no escribir
            for k in muertos:
                q.pop(k, None)
            return q
        self._store.update_cross_process(fn, {})
        d = self._store.read()
        return d if isinstance(d, dict) else {}

    # ---------- API (misma forma que la era de threads) ----------

    def register(self, key, entry):
        """True si registró (no existía); False si ya había (→ encolar)."""
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            if key in q:
                out["ok"] = False
                return None
            e = dict(entry or {})
            e.setdefault("t0", time.time())
            e["pid"] = os.getpid()
            q[key] = e
            out["ok"] = True
            return q
        self._store.update_cross_process(fn, {})
        return out.get("ok", False)

    def rekey(self, old, new):
        """Mover la entrada (ej. 'nueva' → sid real tras session/create)."""
        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            ent = q.pop(old, None)
            if ent is not None and new and new not in q:
                q[new] = ent
            return q
        self._store.update_cross_process(fn, {})
        with self._lock:
            if old in self._clients:
                self._clients[new] = self._clients.pop(old)

    def get(self, key):
        return self._prune().get(key)

    def pop(self, key):
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            out["ent"] = q.pop(key, None)
            return q if out["ent"] is not None else None
        self._store.update_cross_process(fn, {})
        with self._lock:
            self._clients.pop(key, None)
        return out.get("ent")

    def items(self):
        """Snapshot ordenado por t0 (numeración estable de /stop)."""
        d = self._prune()
        return sorted(((k, v) for k, v in d.items()
                       if isinstance(v, dict)),
                      key=lambda kv: kv[1].get("t0") or 0)

    def empty(self):
        return not self.items()

    def set_client(self, key, client):
        """Transición F16: el ProtoClient vive en la memoria de ESTE
        proceso (los pipes no cruzan procesos). Se elimina con el /stop
        por marker."""
        with self._lock:
            self._clients[key] = client

    def flag_stop(self, key):
        """Marca stopped en el archivo y devuelve (client, titulo) o None."""
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            w = q.get(key)
            if not isinstance(w, dict):
                return None
            w = dict(w)
            w["stopped"] = True
            q[key] = w
            out["titulo"] = w.get("titulo")
            return q
        self._store.update_cross_process(fn, {})
        if "titulo" not in out:
            return None
        with self._lock:
            return self._clients.get(key), out["titulo"]

    def unflag_stop(self, key):
        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            w = q.get(key)
            if not isinstance(w, dict):
                return None
            w = dict(w)
            w["stopped"] = False
            q[key] = w
            return q
        self._store.update_cross_process(fn, {})

    def stopped(self, key):
        ent = self._prune().get(key)
        return bool(isinstance(ent, dict) and ent.get("stopped"))


class PendingQuestion:
    """Una pregunta del agente esperando respuesta del usuario."""

    def __init__(self, request_id, questions, titulo, sid=None):
        self.request_id = request_id
        self.questions = questions
        self.titulo = titulo
        self.sid = sid  # F15: enruta la respuesta a la sesión que preguntó
        self.event = threading.Event()
        self.answer = None


class QuestionsBroker:
    """Reemplaza el slot global único de respuestas (carrera con 2
    preguntas simultáneas): cada pregunta tiene SU slot correlacionado
    por request_id."""

    def __init__(self):
        self._d = {}
        self._lock = threading.RLock()

    def ask(self, request_id, questions, titulo, sid=None):
        q = PendingQuestion(request_id, questions, titulo, sid)
        with self._lock:
            self._d[request_id] = q
        return q

    def answer(self, request_id, text):
        """Responde la pregunta request_id. True si estaba pendiente."""
        with self._lock:
            q = self._d.get(request_id)
            if not q or q.event.is_set():
                return False
            q.answer = text
            q.event.set()
            return True

    def first_pending(self, sid=None):
        """F15: prefiere la pregunta de la sesión indicada (la task current);
        si ninguna calza, la primera pendiente (comportamiento previo)."""
        with self._lock:
            pend = [q for q in self._d.values() if not q.event.is_set()]
            if not pend:
                return None
            for q in pend:
                if sid and q.sid == sid:
                    return q
            return pend[0]

    def any_pending(self):
        with self._lock:
            return any(not q.event.is_set() for q in self._d.values())

    def pop(self, request_id):
        with self._lock:
            return self._d.pop(request_id, None)
