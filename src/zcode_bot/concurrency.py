"""concurrency — WorkersRegistry (patrón Registry) y QuestionsBroker
(correlación request/response por id).

Regla de oro (lección del deadlock del 2026-10-12): threading.Lock NO es
reentrante. Estos tipos expone SOLO métodos que toman el lock una vez y
nunca llaman a otro método que lo tome (RLock como cinturón de seguridad).

WorkersRegistry reemplaza el dict global HEADLESS_WORKERS: antes, 14 puntos
de acceso de los cuales solo 2 con lock (revisión F0) y un bug de orphan
(pop bajo la clave equivocada tras crear sesión). Ahora TODO acceso pasa
por métodos con lock.
"""
import threading


class WorkersRegistry:
    """sid -> {t0, prompt, ws, titulo, client, stopped} con lock total."""

    def __init__(self):
        self._d = {}
        self._lock = threading.RLock()

    def register(self, key, entry):
        """True si registró (no existía); False si ya había (→ encolar)."""
        with self._lock:
            if key in self._d:
                return False
            self._d[key] = entry
            return True

    def rekey(self, old, new):
        """Mover la entrada (ej. 'nueva' → sid real tras session/create)."""
        with self._lock:
            ent = self._d.pop(old, None)
            if ent is not None and new and new not in self._d:
                self._d[new] = ent

    def get(self, key):
        with self._lock:
            return self._d.get(key)

    def pop(self, key):
        with self._lock:
            return self._d.pop(key, None)

    def items(self):
        with self._lock:
            return list(self._d.items())

    def empty(self):
        with self._lock:
            return not self._d

    def set_client(self, key, client):
        with self._lock:
            if key in self._d:
                self._d[key]["client"] = client

    def flag_stop(self, key):
        """Marca stopped y devuelve (client, titulo) o None si no existe."""
        with self._lock:
            w = self._d.get(key)
            if not w:
                return None
            w["stopped"] = True
            return w.get("client"), w.get("titulo")

    def unflag_stop(self, key):
        with self._lock:
            w = self._d.get(key)
            if w:
                w["stopped"] = False

    def stopped(self, key):
        with self._lock:
            w = self._d.get(key)
            return bool(w and w.get("stopped"))


class PendingQuestion:
    """Una pregunta del agente esperando respuesta del usuario."""

    def __init__(self, request_id, questions, titulo):
        self.request_id = request_id
        self.questions = questions
        self.titulo = titulo
        self.event = threading.Event()
        self.answer = None


class QuestionsBroker:
    """Reemplaza el slot global único de respuestas (carrera con 2
    preguntas simultáneas): cada pregunta tiene SU slot correlacionado
    por request_id."""

    def __init__(self):
        self._d = {}
        self._lock = threading.RLock()

    def ask(self, request_id, questions, titulo):
        q = PendingQuestion(request_id, questions, titulo)
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

    def first_pending(self):
        with self._lock:
            for q in self._d.values():
                if not q.event.is_set():
                    return q
        return None

    def any_pending(self):
        with self._lock:
            return any(not q.event.is_set() for q in self._d.values())

    def pop(self, request_id):
        with self._lock:
            return self._d.pop(request_id, None)
