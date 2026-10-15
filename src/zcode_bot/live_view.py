"""live_view — el sidecar de un turno headless (F4d).

LiveTurnView: la vista en vivo del turno (caja viva de razonamiento,
cajas permanentes de CoT, caja de pasos). Antes era la función _live_view
del gateway con 22 locals mutados a través del loop y una closure con
nonlocals — el incidente «Probando cot 1234» (14/10) fue un nonlocal
olvidado. Como clase, el bug desaparece por construcción: el estado son
atributos y cot_flush es un método.

Familia: _turn_end_poll (espera reactiva del fin del turno), guardas
anti-OOM (_poll_cadence, _rss_*, _live_skip_*), _headless_text y las
cajas de tools. Transporte POR ATRIBUTO (uhttp.send): un solo seam de
parcheo para toda la suite.
"""
import json
import os
import subprocess
import time

from zcode_bot import config
from zcode_bot import cli_logs as zc_logs
from zcode_bot import fswatch
from zcode_bot.core import _stop_requested, _task_current_ok, _log_watch_paths
from zcode_bot.messages import (_fetch_messages, _last_id_of, _tool_caja,
                                _steps_of_messages, _tools_of_messages)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, fmt_tok
from zcode_bot.utils.logging import pwlog

# ---------------------------------------------------------------- trace

COT_CHUNK = 3000   # por mensaje: esc() puede inflar el texto, margen s/ 4096
COT_TAIL = 3200    # cola que se mantiene editable en la caja viva
EVENTS_TRIP = 1200  # replay mayor a esto → live OFF (anti-OOM, ver LiveTurnView)
LIVE_FRAME_TRIP = 25 * 1024 * 1024  # un frame de session/events mayor a 25MB
# significa replay con imágenes (caso real: 3.2GB de pico de RSS en UNA
# lectura con apenas 329 eventos) → live OFF + flag por sesión
LIVE_SKIP_TTL = 6 * 3600  # el flag vive 6 h; el compact achica el replay y
# lo limpia (ver zcode_compact), tras lo cual el live vuelve solo
LIVE_BIG_FRAME = 2 * 1024 * 1024  # frame >2MB → cadenciar el poll (el
# protocolo RECHAZA afterEventId — verificado: ZodError unrecognized_keys;
# sin incremental disponible, la única palanca es la frecuencia)


def _poll_cadence(max_frame):
    """Cadencia del session/events según el tamaño visto: el server re-envía
    el replay COMPLETO en cada llamada (sin afterEventId), así que un frame
    grande solo se puede amortiguar polleando menos seguido."""
    return 5.0 if max_frame > LIVE_BIG_FRAME else 2.0


def _rss_now_mb(run=None):
    """RSS ACTUAL del proceso (ps) — ru_maxrss es pico histórico y no
    distingue transitorio de retenido; esto sí. El poller NO corre bajo
    sandbox (solo sus runtimes), así que ps le funciona."""
    run = run or subprocess.run
    try:
        out = run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                  capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) // 1024
    except (OSError, ValueError):
        return -1


def _rss_mb():
    """RSS máximo del proceso en MB (ru_maxrss viene en bytes en macOS)."""
    import resource
    try:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576)
    except Exception:
        return -1


def _turn_end_poll(sid, t0, timeout=1800, stop_check=None, t_send=None):
    """Espera turn.completed/failed de sid (ts >= t0). Lee solo la COLA del
    último log (512KB).

    F20 — correlación por turnId: con t_send (post session/send aceptado),
    el primer turn.started con ts >= t_send es EL turno del worker y se
    espera el fin de ESE turnId. Antes cualquier turn.completed de la
    sesión servía: con la app corriendo un turno en la MISMA sesión
    (peer), el worker tomaba el fin del turno ajeno y entregaba SU
    respuesta como propia (incidente 18:29: entregó el texto de la Mac
    como «el plan» y el turno real quedó huérfano).

    REACTIVO (F16): con kqueue el thread duerme en el kernel y despierta
    cuando el CLI escribe al log. stop_check() corre en cada despertar
    (gancho del /stop por marker — el worker frena SU turno)."""
    deadline = time.time() + timeout
    mios = None  # turnId del turno que disparó NUESTRO session/send
    last_scan = 0.0
    while time.time() < deadline:
        if stop_check is not None:
            try:
                stop_check()
            except Exception:  # noqa: BLE001
                pass
        now = time.time()
        if now - last_scan >= 1.0:  # throttle: el log escribe en ráfaga
            last_scan = now
            fin = None
            for _ts, _s, _ev, d in zc_logs.iter_events(
                    config.BASE, needles=("turn.started", "turn.completed",
                                   "turn.failed"),
                    sids={sid}, since_epoch=t0, last_files=1,
                    tail_bytes=512 * 1024):
                ev = d.get("event")
                if ev == "turn.started":
                    if mios is None and (t_send is None or _ts >= t_send - 1):
                        mios = d.get("turnId")
                elif ev in ("turn.completed", "turn.failed"):
                    if d.get("turnId") == mios or (mios is None
                                                   and t_send is None):
                        fin = d
            if fin is not None:
                return fin
        # recv reactivo del mailbox: despertar en la próxima escritura del
        # log (el debounce de fswatch coalescea ráfagas)
        wait_s = max(0.05, min(4.0, deadline - time.time()))
        fswatch.wait_change(_log_watch_paths(), timeout=wait_s)


def _live_skip_path():
    return os.path.join(config.STATE_DIR, "bot-live-skip.json")


def _live_skip_all():
    try:
        with open(_live_skip_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _live_skip_mark(sid, now=None):
    """Marca la sesión como 'replay gigante': el próximo turno arranca con
    el CoT en vivo APAGADO (ni un parseo de GBs — pasos y documento final
    siguen igual). Idempotente y best-effort."""
    now = now or time.time()
    d = _live_skip_all()
    d[sid] = now
    tmp = _live_skip_path() + ".tmp"
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _live_skip_path())
    except OSError:
        pass


def _live_skip_check(sid, now=None):
    """True si la sesión tiene flag fresco (TTL LIVE_SKIP_TTL)."""
    ts = _live_skip_all().get(sid)
    return bool(ts) and (now or time.time()) - ts < LIVE_SKIP_TTL


def _live_skip_clear(sid):
    d = _live_skip_all()
    if sid in d:
        del d[sid]
        tmp = _live_skip_path() + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, _live_skip_path())
        except OSError:
            pass


HEADLESS_GUARD = (
    "\n\n—\n⚠️ (aviso del bot que ejecuta este mensaje: corrés HEADLESS dentro "
    "de un runtime zcode propio. NO mates ni reinicies procesos de zcode — "
    "kill/pkill/killall sobre zcode o app-server matan a TU PROPIO huésped y "
    "este turno muere con «Turn was cancelled» (incidentes reales 2026-10-13 "
    "14:04, 15:12 y 16:13). launchctl ya está BLOQUEADO en tu sandbox: ni lo "
    "intentes. Si el bot o el poller parecen trabados, NO los reinicies: "
    "avisale al usuario por el chat y que use /nuclear desde Telegram. Para "
    "deployar cambios: commit + push y avisarle — deploy.sh desde acá difiere "
    "la recarga solo.)"
)


def _headless_text(text, cfg):
    """Texto final del prompt headless: guardrail anti-suicidio de runtime.

    Incidente 15:12: un turno headless hizo `kill -TERM <pid>` sobre su
    propio runtime (creyó que era un "worker duplicado") y se canceló a sí
    mismo. Se APENDEA (no se antepone) para no tocar el arranque del prompt
    y se deduplica por si el texto ya lo trae (reenvío de cola).
    """
    if not cfg.get("headless_guard", True):
        return text
    if HEADLESS_GUARD[:20] in (text or "")[-600:]:
        return text
    return (text or "") + HEADLESS_GUARD


def _cot_chunks_to_send(cot_full, flushed, tail_keep=COT_TAIL,
                        ch=COT_CHUNK):
    """Trozos listos para mandar como mensajes PERMANENTES de CoT: todo lo
    acumulado que excede la cola viva (tail_keep), en piezas de ch. La cola
    sigue editándose aparte — así el CoT queda scrolleable en el chat
    mientras corre el turno (límite 4096/mensaje = el porqué de los trozos).
    SIN TOPE: Damian pidió que se manden TODOS (el pace de 1.2 s del flush
    mantiene el chat dentro del ~1 msg/s de Telegram)."""
    out = []
    while len(cot_full) - flushed > tail_keep:
        trozo = cot_full[flushed:flushed + ch]
        out.append(trozo)
        flushed += len(trozo)
    return out, flushed


def _cajas_paso_nuevas(cfg, titulo, contador, nuevos):
    """F22bis — manda como mensajes PERMANENTES (scrolleables como las de
    CoT) cada tool call nuevo: comando completo + diff. Config
    tools_stream=false apaga."""
    if not cfg.get("tools_stream", True):
        return
    for nombre, inp in nuevos:
        contador[0] += 1
        try:
            uhttp.send(cfg, "%s · <b>%d</b>" % (_tool_caja(nombre, inp),
                                                contador[0]), loud=False)
            if contador[0] % 3 == 0:
                time.sleep(1.0)  # pace ~1 msg/s del chat
        except Exception:  # noqa: BLE001
            pass


class LiveTurnView:
    """Sidecar de un turno headless. Tres mensajes vivos/permanentes:
    1) caja viva del razonamiento/texto (deltas de session/events, dedupe
       por eventId, sin acumular payloads — guardas de tamaño de la fuga).
    2) cajas PERMANENTES de CoT: al llenarse la cola (4096/mensaje es límite
       duro de Telegram) el excedente se manda como mensajes fijos «CoT N»
       — el razonamiento queda scrolleable de principio a fin MIENTRAS
       corre el turno (config cot_stream=false lo apaga).
    3) «pasos»: los tool calls completados del historial (afterMessageId)
       — 🔧 comandos, archivos, tools.

    deps: todo efecto externo es inyectable (send/live_send/live_edit/
    sleep/now/pwlog/fetch/task_current_ok/stop_requested/skip_check/
    skip_mark/cajas_paso/poll_cadence/rss/rss_now) — testeable sin red
    y sin parchear time.sleep global (F4d).
    """

    def __init__(self, c, cfg, sid, t0, titulo, msg_id, th, last_id=None,
                 deps=None):
        d = deps or {}
        self.c, self.cfg, self.sid = c, cfg, sid
        self.t0, self.titulo, self.th = t0, titulo, th
        # --- deps inyectables (defaults = módulo; tests pasan recorders) ---
        self.send = d.get("send", uhttp.send)
        self.live_send = d.get("live_send", uhttp._live_send)
        self.live_edit = d.get("live_edit", uhttp._live_edit)
        self.sleep = d.get("sleep", time.sleep)
        self.now = d.get("now", time.time)
        self.pwlog = d.get("pwlog", pwlog)
        self.fetch_messages = d.get("fetch_messages", _fetch_messages)
        self.task_current_ok = d.get("task_current_ok", _task_current_ok)
        self.stop_requested = d.get("stop_requested", _stop_requested)
        self.skip_check = d.get("skip_check", _live_skip_check)
        self.skip_mark = d.get("skip_mark", _live_skip_mark)
        self.cajas_paso = d.get("cajas_paso", _cajas_paso_nuevas)
        self.poll_cadence = d.get("poll_cadence", _poll_cadence)
        self.rss = d.get("rss", _rss_mb)
        self.rss_now = d.get("rss_now", _rss_now_mb)
        # --- contexto del turno ---
        self.msg_id = msg_id
        self.deadline = time.time() + 2100
        # --- estado de stream (ex-locals de _live_view) ---
        self.razon = ""      # solo la fase de pensado EN CURSO (se resetea)
        self.razon_n = 0     # chars totales pensados (para el log)
        self.texto = ""      # COLA de lo escrito (últimos 8KB, caja viva)
        self.texto_n = 0     # chars totales escritos
        self.n_deltas = 0
        self.vistos = set()
        self.pasos = []
        self.pasos_nmsg = [0]  # lista-de-1: así lo consume _cajas_paso_nuevas
        self.ph_pasos = None
        self.pasos_last = last_id
        self.last_edit = 0.0
        self.last_poll_pasos = 0.0
        self.cot_full = ""    # CoT pendiente de mandar (lo enviado se LIBERA)
        self.cot_flushed = 0  # chars ya enviados como mensajes permanentes
        self.cot_nmsg = 0
        self.live_id = msg_id  # caja viva: se MUDA al primer flush de CoT
        self.live_moved = False
        self.evento_write = False
        self.primer_poll = True  # instrumentación anti-OOM (primer replay)
        self.events_off = False  # anti-OOM: session/events devuelve el replay
        # COMPLETO de la sesión en cada llamada; en sesiones gigantes,
        # parsearlo cada 2s infla la RSS hasta que macOS mata el poller
        # (2.9GB real, turno perdido).
        if self.skip_check(sid):
            # flag de un turno anterior: el replay de ESTA sesión ya pesa
            # GBs (imágenes) — ni un solo parseo; pasos y documento siguen
            self.events_off = True
            self.pwlog("live events OFF de arranque (flag replay gigante)")
        self.cad = 2.0  # cadencia del poll (adaptiva: frame grande → 5 s)
        self.segundo_plano = False  # F15: dejó de ser current a mitad de turno
        self.stop_pedidos = 0       # F16: /stop por marker — UNA sola vez

    # ------------------------------------------------- flush de CoT

    def _cot_flush(self, tail_keep, pace=True):
        """Manda como mensajes PERMANENTES el CoT ya acumulado (cajas que
        quedan scrolleables arriba). Pausado para respetar el ~1 mensaje/s
        por chat de la FAQ de Telegram. SIN tope: se mandan todos."""
        if not self.cfg.get("cot_stream", True):
            return
        trozos, nf = _cot_chunks_to_send(
            self.cot_full, self.cot_flushed, tail_keep=tail_keep)
        for i, trozo in enumerate(trozos):
            if i and pace:
                self.sleep(1.2)
            self.cot_nmsg += 1
            self.send(self.cfg, "🧠 <b>«%s»</b> · CoT %d\n<pre>%s</pre>"
                      % (esc(self.titulo), self.cot_nmsg, esc(trozo)),
                      loud=False)
        if nf:
            # MEMORIA: lo ya entregado al chat no se retiene — el prefijo
            # enviado se suelta y el documento final sale del historial
            self.cot_full = self.cot_full[nf:]
            self.cot_flushed = 0
        if trozos and not self.live_moved and self.live_id:
            # el "en vivo" se muda al final del chat: la caja viva queda
            # ABAJO de las cajas congeladas, donde se está leyendo
            self.live_moved = True
            self.live_id = (self.live_send(
                self.cfg, "🧠 <b>«%s»</b> — pensando…"
                % esc(self.titulo)) or self.live_id)

    # ------------------------------------------------- fase eventos

    def _poll_events(self):
        if self.events_off:
            # trip activado: no volver a pedir el replay gigante — los
            # pasos y el documento final cubren el resto del turno
            return
        r = self.c.request("session/events", {"sessionId": self.sid},
                           timeout=20)
        evs = (r.get("result") or {}).get("events") or []
        if self.primer_poll:
            self.primer_poll = False
            self.cad = self.poll_cadence(self.c.max_frame)
            self.pwlog("live evs=%d frame=%dMB rss=%dMB rss_now=%dMB cad=%.0fs"
                       % (len(evs), self.c.max_frame // 1048576, self.rss(),
                          self.rss_now(), self.cad))
            if self.c.max_frame > LIVE_FRAME_TRIP:
                # UNA lectura ya pagó el parseo gigante: no repetirlo cada
                # 2 s — apagar y marcar la sesión para los próximos turnos
                # (el compact la limpia)
                self.events_off = True
                self.skip_mark(self.sid)
                self.pwlog("live events OFF: frame de %dMB con %d evs "
                           "(replay con imágenes) — flag %dh por sesión"
                           % (self.c.max_frame // 1048576, len(evs),
                              LIVE_SKIP_TTL // 3600))
        if len(evs) > EVENTS_TRIP:
            self.events_off = True
            self.pwlog("live events OFF: replay %d evs (anti-OOM) — "
                       "sigo con pasos+documento" % len(evs))
            return
        # buffer re-enviado entero en cada llamada: solo los NUEVOS
        for ev in evs[-400:]:
            eid = ev.get("eventId")
            if not eid or eid in self.vistos:
                continue
            self.vistos.add(eid)
            if len(json.dumps(ev)) > 30000:  # snapshot: ignorar
                continue
            p = ev.get("payload") or {}
            k = p.get("kind") or p.get("type") or ""
            if k == "reasoning_delta" and p.get("delta"):
                dd = p["delta"]
                self.razon = (self.razon + dd)[-8192:]  # cola: el íntegro
                self.razon_n += len(dd)                  # vive en cot_full
                self.cot_full += dd
                self.n_deltas += 1
            elif k == "text_delta" and p.get("delta"):
                dd = p["delta"]
                self.texto = (self.texto + dd)[-8192:]  # cola para la caja
                self.texto_n += len(dd)                  # viva; el documento
                self.n_deltas += 1                       # final sale del store
                if self.razon and not self.evento_write:
                    # EVENTO DE FASE: pasó de pensar a escribir — mensaje
                    # propio, abajo de las cajas de CoT
                    self.evento_write = True
                    if self.cfg.get("cot_stream", True) \
                            and not self.segundo_plano:
                        self.send(self.cfg,
                                  "🧠 <b>«%s»</b> — análisis terminado "
                                  "(%s chars de CoT · %d cajas)\n✍️ ahora "
                                  "escribiendo la respuesta…"
                                  % (esc(self.titulo),
                                     fmt_tok(len(self.cot_full)),
                                     self.cot_nmsg), loud=False)
                self.razon = ""  # empezó a escribir: el CoT ya cumplió

    # ------------------------------------------------- fase pasos

    def _poll_pasos(self):
        self.last_poll_pasos = self.now()
        try:
            nuevos = self.fetch_messages(self.c, self.sid,
                                         after_id=self.pasos_last, limit=30)
            if nuevos:
                self.pasos_last = _last_id_of(nuevos) or self.pasos_last
                self.pasos.extend(_steps_of_messages(nuevos))
                # F22bis: cajas PERMANENTES de cada tool call nuevo (comando
                # completo / diff) — `nuevos` ya es incremental (after_id)
                if not self.segundo_plano:
                    try:
                        self.cajas_paso(self.cfg, self.titulo, self.pasos_nmsg,
                                        _tools_of_messages(nuevos))
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------- caja viva

    def _edit_live_box(self):
        self.last_edit = self.now()
        # cajas PERMANENTES de CoT: lo que ya no cabe en la cola viva se
        # manda como mensaje fijo (scrolleable de principio a fin)
        self._cot_flush(COT_TAIL)
        trans = int(self.now() - self.t0)
        if self.texto:
            cuerpo = ("⚙️ <b>«%s»</b> · %ds\n<pre>%s</pre>\n✍️ escribiendo…"
                      % (esc(self.titulo), trans, esc(self.texto[-3200:])))
        elif self.events_off:
            cuerpo = ("📄 <b>«%s»</b> · %ds\nCoT en vivo pausado "
                      "(sesión muy grande para el stream — protege la "
                      "memoria del bot; /compact lo reactiva).\n"
                      "🔧 pasos activos · el documento final trae el "
                      "CoT COMPLETO."
                      % (esc(self.titulo), trans))
        elif self.razon:
            cuerpo = ("🧠 <b>«%s»</b> · %ds\n<b>pensando:</b>\n<pre>%s</pre>"
                      % (esc(self.titulo), trans, esc(self.razon[-3200:])))
        elif self.n_deltas == 0 and trans > 12:
            # sin deltas = el turno lo ejecuta la app de la Mac (task
            # abierta ahí): su streaming interno no cruza al bot
            cuerpo = ("🖥️ <b>«%s»</b> · %ds\ncorriendo en la Mac "
                      "(task abierta en la app) — te aviso al terminar"
                      % (esc(self.titulo), trans))
        else:
            cuerpo = ("🧠 <b>«%s»</b> · %ds\npensando…"
                      % (esc(self.titulo), trans))
        self.live_edit(self.cfg, self.msg_id, cuerpo)
        if self.pasos:
            if self.ph_pasos is None:
                self.ph_pasos = self.live_send(
                    self.cfg, "🔧 <b>«%s»</b> — pasos:" % esc(self.titulo))
            if self.ph_pasos:
                ult = self.pasos[-12:]
                self.live_edit(self.cfg, self.ph_pasos,
                               "🔧 <b>«%s»</b> — %d pasos:\n%s"
                               % (esc(self.titulo), len(self.pasos),
                                  "\n".join(
                                      "• %s%s" % (esc(n),
                                                  " · " + esc(d) if d else "")
                                      for n, d in ult)
                                  + ("\n···" if len(self.pasos) > 12 else "")))

    # ------------------------------------------------- loop principal

    def run(self):
        while self.now() < self.deadline:
            if not self.th.is_alive():
                break
            if self.stop_pedidos < 1 and self.stop_requested(self.sid):
                # /stop: frenar con el runtime PROPIO del worker (stop
                # real). El turno termina enseguida y th corta el loop.
                self.stop_pedidos += 1
                self.pwlog("stop marker: session/stop %s" % self.sid)
                try:
                    self.c.request("session/stop", {"sessionId": self.sid},
                                   timeout=30)
                except Exception:  # noqa: BLE001
                    pass
            if not self.segundo_plano \
                    and not self.task_current_ok(self.cfg, self.sid):
                # F15: silenciar el stream (CoT/cajas/pasos) con UN edit de
                # despedida; el turno sigue y la RESPUESTA FINAL igual llega
                # (prompt_worker) — es respuesta al mensaje del usuario
                self.segundo_plano = True
                self.pwlog("live pausado: %s dejó de ser la task current"
                           % self.sid)
                try:
                    self.live_edit(self.cfg, self.msg_id,
                                   "⏸️ <b>«%s»</b> — siguió en segundo plano "
                                   "(cambiaste de task); aviso al terminar"
                                   % esc(self.titulo))
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._poll_events()
            except Exception:  # noqa: BLE001 - el en vivo jamás rompe el turno
                pass
            # pasos: cada PASO del turno es un mensaje assistant que
            # completa — poll liviano del historial nuevo cada 6 s
            if self.now() - self.last_poll_pasos >= 6.0:
                self._poll_pasos()
            if self.live_id and not self.segundo_plano \
                    and self.now() - self.last_edit >= 3.0:
                self._edit_live_box()
            self.sleep(self.cad)  # adaptativa: 2 s normal, 5 s frame grande
        # fin del turno: el CoT restante también a mensajes permanentes —
        # el chat queda con el razonamiento COMPLETO scrolleable
        if not self.segundo_plano:  # F15: backgrounded = sin lluvia de cierre
            try:
                self._cot_flush(0, pace=False)
            except Exception:  # noqa: BLE001
                pass
        self.pwlog("live fin: deltas=%d razon=%d texto=%d pasos=%d cot_msgs=%d"
                   % (self.n_deltas, self.razon_n, self.texto_n,
                      len(self.pasos), self.cot_nmsg))
        # [1] es la COLA del texto (8KB) — el texto íntegro sale del historial
        return self.cot_full, self.texto, self.pasos, self.live_id
