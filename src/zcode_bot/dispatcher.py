"""dispatcher — el ÚNICO procesador de updates del bot.

Sucesor del "un solo lector de getUpdates" (ADR-001) para la era async:
cola SERIAL + dedup por update_id + gate de owner. Polling (offset
confirmado post-dispatch) y webhook (Telegram reintenta ante no-200 →
dedup obligatorio) comparten ESTA pieza: cada update se procesa una vez,
en orden de llegada, uno a la vez — la semántica que todo el estado del
bot (tg-busy, mailboxes de archivo, sesión activa) asume desde F16.

El procesamiento es el handler SINCRÓNICO del bot (puede bloquear
minutos: wait_reply, compact) → corre en el executor (to_thread) y el
loop queda libre para el webhook server y tareas supervisoras.
"""
import asyncio
import collections
import time


class UpdateDispatcher:
    """Serializa updates: `submit` encola, `run` consume de a uno.

    Dedup at-least-once: el update_id se marca visto AL ENCOLAR (rechazo
    rápido de ráfagas) y se LIBERA si el procesamiento lanzó excepción —
    así un reintento de Telegram puede reprocesarlo (duplicar es mejor
    que perder, misma decisión del FIX 14/10 del offset).
    """

    def __init__(self, process, owner_ok, seen_ttl=900.0, seen_max=10000,
                 on_error=None):
        self._process = process        # (cfg, msg) -> None; sync, bloqueante
        self._owner_ok = owner_ok      # (cfg, msg) -> bool, fail-closed
        self._seen_ttl = seen_ttl
        self._seen_max = seen_max
        self._on_error = on_error      # exc -> None (log); None = tragar
        self._seen = collections.OrderedDict()   # update_id -> ts (monotonic)
        self._q = asyncio.Queue()
        self.aceptados = 0
        self.duplicados = 0
        self.rechazados = 0

    # ------------------------------------------------------------ dedup
    def _purge_expired(self):
        now = time.monotonic()
        viejos = [k for k, ts in self._seen.items()
                  if now - ts > self._seen_ttl]
        for k in viejos:
            del self._seen[k]

    def _mark_seen(self, update_id):
        self._purge_expired()
        self._seen[update_id] = time.monotonic()
        self._seen.move_to_end(update_id)
        while len(self._seen) > self._seen_max:
            self._seen.popitem(last=False)

    def already_seen(self, update_id):
        self._purge_expired()
        return update_id in self._seen

    # ---------------------------------------------------------- entrada
    async def submit(self, cfg, msg):
        """Encola si es nuevo y del owner. Devuelve (aceptado, motivo)."""
        uid = msg.get("update_id")
        if uid is not None and uid in self._seen:
            self.duplicados += 1
            return False, "duplicado"
        if not self._owner_ok(cfg, msg):
            self.rechazados += 1
            return False, "no-owner"
        if uid is not None:
            self._mark_seen(uid)
        self.aceptados += 1
        await self._q.put((cfg, msg))
        return True, "ok"

    async def drain(self):
        """Espera a que TODO lo encolado esté procesado (el polling
        confirma el offset recién acá — FIX 14/10: post-dispatch)."""
        await self._q.join()

    def pending(self):
        return self._q.qsize()

    # --------------------------------------------------------- consumo
    async def run(self):
        """Consumidor serial: un update por vez, en orden. Nunca muere."""
        while True:
            cfg, msg = await self._q.get()
            uid = msg.get("update_id")
            try:
                await asyncio.to_thread(self._process, cfg, msg)
            except Exception as exc:  # noqa: BLE001 - el dispatcher no muere
                if uid is not None:
                    self._seen.pop(uid, None)  # reprocesable por reintento
                if self._on_error:
                    self._on_error(exc)
            finally:
                self._q.task_done()
