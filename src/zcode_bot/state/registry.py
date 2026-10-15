"""concurrency — WorkersRegistry (patrón Registry, file-backed) y
QuestionFiles (correlación request/response por id, file-based).

Regla de oro (lección del deadlock del 2026-10-12): threading.Lock NO es
reentrante. Los locks que quedan se toman una vez por método público.

F16: ambos contratos viven en archivos bajo state/ con flock — poller y
workers-PROCESO comparten la misma vista, y un restart del poller ya no
borra los turnos en vuelo (el bug estructural de los «Turn was
cancelled» del 2026-10-13). Entradas de workers podadas por PID muerto
(os.kill(pid,0)) con tope de edad anti PID-reuse.
"""
import json
import os
import time

from .store import AtomicStore


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
        self._store = AtomicStore(path)

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

    def get(self, key):
        return self._prune().get(key)

    def pop(self, key):
        out = {}

        def fn(q):
            q = dict(q) if isinstance(q, dict) else {}
            out["ent"] = q.pop(key, None)
            return q if out["ent"] is not None else None
        self._store.update_cross_process(fn, {})
        return out.get("ent")

    def items(self):
        """Snapshot ordenado por t0 (numeración estable de /stop)."""
        d = self._prune()
        return sorted(((k, v) for k, v in d.items()
                       if isinstance(v, dict)),
                      key=lambda kv: kv[1].get("t0") or 0)

    def empty(self):
        return not self.items()

    def flag_stop(self, key):
        """Marca stopped en el archivo y devuelve el titulo, o None si no
        existe la entrada. (F16: ya no devuelve client — el frenado real
        lo hace el worker con SU runtime vía el marker de /stop.)"""
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
        return out.get("titulo")

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


class QuestionFiles:
    """F16 — preguntas ❓ worker→poller por ARCHIVO (state/bot-q/).

    El callback de interaction/requestUserInput corre en el proceso del
    worker; la respuesta llega del poller (botón/número del chat). El
    threading.Event in-memory no cruza procesos: el contrato es
    q-<rid>.json (pregunta pendiente) + a-<rid>.json (respuesta).

    - ask():    el worker escribe q y arma el mensaje del chat.
    - wait():   poll del a cada 2 s (timeout → decline); limpia sus files.
    - answer(): el poller escribe a y borra q (True si había pregunta).
    - pendings()/first_pending(sid): routing por sesión (F15).
    - Poda por edad: una q de un worker crasheado no vive para siempre.
    """

    def __init__(self, dirpath):
        self.dir = dirpath

    def _p(self, kind, rid):
        safe = "".join(c if (c.isalnum() or c in "._-") else "-"
                       for c in str(rid))
        return os.path.join(self.dir, "%s-%s.json" % (kind, safe))

    def ask(self, rid, questions, titulo, sid=None):
        os.makedirs(self.dir, exist_ok=True)
        obj = {"request_id": str(rid), "questions": questions or [],
               "titulo": titulo, "sid": sid, "t0": time.time()}
        with open(self._p("q", rid), "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        return obj

    def answer(self, rid, text):
        """Escribe la respuesta y retira la pregunta. True si había."""
        qp = self._p("q", rid)
        if not os.path.exists(qp):
            return False
        os.makedirs(self.dir, exist_ok=True)
        with open(self._p("a", rid), "w") as f:
            json.dump({"request_id": str(rid), "answer": text}, f,
                      ensure_ascii=False)
        try:
            os.remove(qp)
        except OSError:
            pass
        return True

    def wait(self, rid, timeout=480.0, poll=2.0):
        """Bloquea hasta la respuesta (devuelve su texto) o timeout (None).

        Semántica de canal de Go: recv BLOQUEANTE que despierta cuando
        llega el mensaje — con kqueue (macOS) el thread duerme en el
        kernel hasta que el dir cambia (cero CPU, ~0 latencia); sin
        kqueue, sondea cada `poll` s. Siempre limpia sus archivos: el
        crash de un lado no deja basura del otro (la q huérfana la poda
        pendings() por edad)."""
        from zcode_bot import fswatch
        ap = self._p("a", rid)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(ap) as f:
                    obj = json.load(f)
                self.pop(rid)
                return obj.get("answer")
            except (OSError, ValueError):
                pass
            # recv reactivo: dormir SOLO hasta que aparezca algo en el dir
            fswatch.wait_change([self.dir], timeout=min(poll,
                    max(0.05, deadline - time.time())))
        self.pop(rid)
        return None

    def pendings(self, max_age_s=3600):
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for n in names:
            if not (n.startswith("q-") and n.endswith(".json")):
                continue
            try:
                with open(os.path.join(self.dir, n)) as f:
                    obj = json.load(f)
                if isinstance(obj, dict) and \
                        time.time() - float(obj.get("t0") or 0) <= max_age_s:
                    out.append(obj)
            except (OSError, ValueError, TypeError):
                continue
        out.sort(key=lambda o: float(o.get("t0") or 0))
        return out

    def any_pending(self):
        return bool(self.pendings())

    def first_pending(self, sid=None):
        """F15: prefiere la pregunta de la sesión indicada (la task
        current); si ninguna calza, la primera por edad."""
        pend = self.pendings()
        for q in pend:
            if sid and q.get("sid") == sid:
                return q
        return pend[0] if pend else None

    def pop(self, rid):
        for p in (self._p("q", rid), self._p("a", rid)):
            try:
                os.remove(p)
            except OSError:
                pass