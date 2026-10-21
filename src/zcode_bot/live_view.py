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
from zcode_bot import razonamiento as cotrz  # F42
from zcode_bot import fswatch
from zcode_bot.core import _stop_requested, _task_current_ok, _log_watch_paths
from zcode_bot.messages import (ToolStep, _fetch_messages, _last_id_of,
                                _tool_caja, _tool_caja_on, _tool_desc_corto,
                                _tools_con_call_id)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, fmt_tok
from zcode_bot.utils.logging import pwlog

# ---------------------------------------------------------------- trace

COT_CHUNK = 3000   # por mensaje: esc() puede inflar el texto, margen s/ 4096
COT_TAIL = 3200    # cola que se mantiene editable en la caja viva
EVENTS_TRIP = 1200  # replay mayor a esto → live OFF (anti-OOM, ver LiveTurnView)
LIVE_FRAME_TRIP = 25 * 1024 * 1024  # un frame >25MB en un poll INCREMENTAL
# significa que afterSeq no funcionó y el server re-replaya la sesión
# entera CADA poll (el régimen que infló un turno real a 3.2GB de RSS) →
# live OFF + flag por sesión. El PRIMER poll está exento: el replay único
# de arranque se paga una vez y se libera (el CoT se muestra completo)
LIVE_SKIP_TTL = 6 * 3600  # el flag vive 6 h; el compact achica el replay y
# lo limpia (ver zcode_compact), tras lo cual el live vuelve solo
_TURNO_LARGO_AVISO_S = 1800  # R0b: cada cuánto avisar "⏱️ lleva N min" en
# la espera SIN tope del fin de turno (timeout=None de _turn_end_poll)
LIVE_BIG_FRAME = 2 * 1024 * 1024  # frame >2MB → cadenciar el poll. F7b: el
# poll es incremental (afterSeq — verificado en el app-server; afterEventId
# sí lo rechaza: ZodError unrecognized_keys), así que un frame grande ya es
# anormal (snapshot/replay) y solo queda amortiguar la frecuencia


def _frame_size(r):
    """Tamaño del frame DE ESTE poll (no el máx. de la conexión: las
    respuestas gigantes del resume/setModel inflaban max_frame y el trip
    mataba un stream de eventos PERFECTAMENTE sano por herencia —
    incidente 15/10: 'live evs=0 frame=25MB' → CoT pausado sin razón)."""
    try:
        return len(json.dumps(r))
    except (TypeError, ValueError):
        return 0


def _poll_cadence(max_frame):
    """Cadencia del session/events según el tamaño visto. F7b: con afterSeq
    el poll es incremental y los frames quedan en KBs; un frame grande es
    señal de replay inesperado → bajar la frecuencia."""
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


def _fin_de_stream(evs, mios, t_send, last_seq):
    """F9 — un poll del stream → (fin|None, mios, last_seq).

    El stream de session/events trae el ciclo de vida del TURNO: `type`
    turn.started (payload con inputId/input/executionStartedAt) y
    turn.completed/failed (payload con inputId/resultType/response,
    spike scripts/spike_f9_turnfin.py). El stream NO trae turnId (la
    clave de los logs del CLI): la correlación F20 se rehace con
    inputId — el primer turn.started posterior a nuestro session/send
    fija mios, y solo el completed/failed con ESE inputId vale."""
    fin = None
    for ev in evs:
        sq = ev.get("seq")
        if isinstance(sq, int) and (last_seq is None or sq > last_seq):
            last_seq = sq
        et = ev.get("type") or ""
        if et not in ("turn.started", "turn.completed", "turn.failed"):
            continue
        p = ev.get("payload") or {}
        if et == "turn.started":
            if mios is not None:
                continue
            ts0 = (p.get("executionStartedAt") or 0) / 1000.0
            if t_send is None or ts0 >= t_send - 1:
                mios = p.get("inputId") or p.get("queryId")
        elif mios is not None:
            # el identificador puede venir por queryId en cualquiera de
            # las dos puntas (mios ya lo resolvió al abrir): comparar el
            # mismo campo, no asumir inputId
            if (p.get("inputId") or p.get("queryId")) == mios:
                fin = ev
        elif t_send is None:
            fin = ev
    if fin is None:
        return None, mios, last_seq
    p = fin.get("payload") or {}
    ok = fin.get("type") == "turn.completed" \
        and str(p.get("resultType") or "success") == "success"
    return ({"event": "turn.completed" if ok else "turn.failed",
             "error": {} if ok else {"message": str(
                 (p.get("error") or {}).get("message") if isinstance(
                     p.get("error"), dict) else
                 (p.get("error") or p.get("resultType") or "turn falló")
             )[:150]}}, mios, last_seq)


def _turn_end_poll(sid, t0, timeout=1800, stop_check=None, t_send=None,
                   conn=None, start_seq=None, aviso=None):
    """Espera turn.completed/failed de sid (ts >= t0).

    F9 — POR EVENTOS cuando hay conexión y cursor (conn+start_seq del
    subscribe previo al send): poll incremental afterSeq (frames en KB,
    misma disciplina anti-OOM del live). El stream no trae turnId: la
    correlación es por inputId (_fin_de_stream). Cualquier cosa que falle
    (conn muerto, request en error, frame gigante = replay) degrada a lo
    de siempre: escanear los logs JSONL del CLI (cola 512KB) — que queda
    como ÚNICO camino cuando no hay conn (y para el supervisor de
    huérfanos, que ve turnos de workers ya muertos).

    F20 — correlación por turnId en el camino de logs: con t_send (post
    session/send aceptado), el primer turn.started con ts >= t_send es EL
    turno del worker y se espera el fin de ESE turnId (incidente 18:29:
    entregó el texto de la Mac como propio).

    REACTIVO (F16): en el camino de logs el thread duerme en el kernel
    (kqueue) y despierta cuando el CLI escribe. stop_check() corre en
    cada vuelta (gancho del /stop por marker).

    R0b — timeout=None = SIN tope: mientras el turno viva, se espera (el
    abandono a los 40 min mataba el runtime: turn.failed "Turn was
    cancelled" el mismo segundo que el worker salía — 20/10 14:55). El
    único corte sigue siendo el fin del turno o el /stop de stop_check.
    aviso(min) se llama cada _TURNO_LARGO_AVISO_S de turno corrido."""
    deadline = None if timeout is None else time.time() + timeout
    mios = None
    last_seq = start_seq
    last_scan = 0.0
    proximo_aviso = t0 + _TURNO_LARGO_AVISO_S
    while deadline is None or time.time() < deadline:
        if stop_check is not None:
            try:
                stop_check()
            except Exception:  # noqa: BLE001
                pass
        if aviso is not None:
            ahora = time.time()
            if ahora >= proximo_aviso:
                try:
                    aviso(int((ahora - t0) // 60))
                except Exception:  # noqa: BLE001 — el aviso jamás corta la espera
                    pass
                while proximo_aviso <= ahora:  # reloj que saltó mucho: alcanzar
                    proximo_aviso += _TURNO_LARGO_AVISO_S
        if conn is not None:
            try:
                params = {"sessionId": sid}
                if last_seq is not None:
                    params["afterSeq"] = last_seq
                r = conn.request("session/events", params, timeout=20)
                tam = _frame_size(r)
                if tam > LIVE_FRAME_TRIP:
                    # replay gigante en el poll del fin = afterSeq roto:
                    # el régimen que infla la RSS — degradar a logs
                    pwlog("fin: stream frame %.1fMB — fallback a logs"
                          % (tam // 1048576))
                    conn = None
                else:
                    evs = (r.get("result") or {}).get("events") or []
                    fin, mios, last_seq = _fin_de_stream(
                        evs, mios, t_send, last_seq)
                    if fin is not None:
                        return fin
            except Exception:  # noqa: BLE001 — el fin jamás rompe el turno
                conn = None  # stream caído: logs por lo que queda de espera
        if conn is None:
            now = time.time()
            if now - last_scan >= 1.0:  # throttle: el log escribe en ráfaga
                last_scan = now
                fin = None
                for _ts, _s, _ev, d in zc_logs.iter_events(
                        config.BASE, needles=("turn.started", "turn.completed",
                                       "turn.failed"),
                        sids={sid}, since_epoch=t0, last_files=1,
                        tail_bytes=zc_logs.LOG_TAIL):
                    ev = d.get("event")
                    if ev == "turn.started":
                        if mios is None and (t_send is None
                                             or _ts >= t_send - 1):
                            mios = d.get("turnId")
                    elif ev in ("turn.completed", "turn.failed"):
                        if d.get("turnId") == mios or (mios is None
                                                       and t_send is None):
                            fin = d
                if fin is not None:
                    return fin
            # recv reactivo del mailbox: despertar en la próxima escritura
            # del log (el debounce de fswatch coalescea ráfagas)
            wait_s = 4.0 if deadline is None else \
                max(0.05, min(4.0, deadline - time.time()))
            fswatch.wait_change(_log_watch_paths(), timeout=wait_s)
        else:
            # R0b: deadline None = sin tope — el pacing no se acorta
            time.sleep(2.0 if deadline is None else
                       max(0.05, min(2.0, deadline - time.time())))


def _live_skip_path():
    return os.path.join(config.STATE_DIR, config.ARCH_LIVE_SKIP)


def _live_skip_all():
    try:
        with open(_live_skip_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _live_skip_write(d):
    """R4 — la escritura atómica del flag (tmp + replace) que
    _live_skip_mark y _live_skip_clear duplicaban. Best-effort."""
    tmp = _live_skip_path() + ".tmp"
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _live_skip_path())
    except OSError:
        pass


def _live_skip_mark(sid, now=None):
    """Marca la sesión como 'replay gigante': el próximo turno arranca con
    el CoT en vivo APAGADO (ni un parseo de GBs — pasos y documento final
    siguen igual). Idempotente y best-effort."""
    now = now or time.time()
    d = _live_skip_all()
    d[sid] = now
    _live_skip_write(d)


def _live_skip_check(sid, now=None):
    """True si la sesión tiene flag fresco (TTL LIVE_SKIP_TTL)."""
    ts = _live_skip_all().get(sid)
    return bool(ts) and (now or time.time()) - ts < LIVE_SKIP_TTL


def _live_skip_clear(sid):
    d = _live_skip_all()
    if sid in d:
        del d[sid]
        _live_skip_write(d)


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
    if not cfg.get("headless_guard"):
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


def _cajas_paso_nuevas(cfg, titulo, contador, nuevos, boxed=None):
    """F22bis — manda como mensajes PERMANENTES (scrolleables como las de
    CoT) cada tool call nuevo: comando completo + diff. Config
    tools_stream=false apaga.
    F7c: `nuevos` son (callID, nombre, inp, status); las tools en `boxed`
    ya tienen caja viva propia (nacieron 🔵 del stream de eventos y se
    cierran por tool.updated.result) — no re-mandar la caja completa."""
    if not cfg.get("tools_stream"):
        return
    boxed = boxed or set()
    for cid, nombre, inp, _status in nuevos:
        if cid and cid in boxed:
            continue
        if not _tool_caja_on(nombre, cfg):  # F7c+: preset de cajas
            continue
        contador[0] += 1
        try:
            uhttp.send(cfg, "%s · <b>%d</b>" % (_tool_caja(nombre, inp),
                                                contador[0]), loud=False)
            if contador[0] % 3 == 0:
                time.sleep(1.0)  # pace ~1 msg/s del chat
        except Exception:  # noqa: BLE001
            pass


class LiveTurnView:
    """Sidecar de un turno headless. Cinco mensajes vivos/permanentes:
    1) caja viva del razonamiento/texto (deltas de session/events, dedupe
       por eventId + cursor afterSeq F7b, sin acumular payloads — guardas
       de tamaño de la fuga) con la tool corriente al lado (F7c).
    2) cajas PERMANENTES de CoT: al llenarse la cola (4096/mensaje es límite
       duro de Telegram) el excedente se manda como mensajes fijos «CoT N»
       — el razonamiento queda scrolleable de principio a fin MIENTRAS
       corre el turno (config cot_stream=false lo apaga).
    3) «pasos»: los tool calls completados del historial (afterMessageId)
       — 🔧 comandos, archivos, tools.
    4) F7c «live action»: una caja por tool que NACE 🔵 apenas el input
       completa (tool_call del stream, input ya parseado — comando entero
       / diff real vía _tool_caja) y se EDITA a ✅/❌ con tool.updated.
       result. La tool corriente se ve tipeándose en la caja viva.
    5) F42-F46 «CoT en botones»: UNA CAJA DE BOTONES POR RAZONADA (rótulos
       «CoT R.C») que nace con la primera caja sellada de esa razonada en
       su posición cronológica, crece mientras vive y cierra con estado
       final propio. F49: sin índice global al final — las cajas que
       quedaron sin mensaje van en UN mensaje de tardías.

    deps: todo efecto externo es inyectable (send/live_send/live_edit/
    sleep/now/pwlog/fetch/task_current_ok/stop_requested/skip_check/
    skip_mark/cajas_paso/poll_cadence/rss/rss_now) — testeable sin red
    y sin parchear time.sleep global (F4d).
    """

    def __init__(self, c, cfg, sid, t0, titulo, msg_id, th, last_id=None,
                 deps=None, start_seq=None):
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
        self.last_seq = start_seq  # F7b: cursor afterSeq (eventSeq del
        # subscribe si el worker lo pasó — salta el replay de arranque)
        self.tools_vivo = {}  # F7c: toolCallId → {nombre, texto, inp,
        # msg_id, fin, salida} — el ciclo de vida visto por el stream
        self.pasos = []
        self.pasos_nmsg = [0]  # lista-de-1: así lo consume _cajas_paso_nuevas
        self.ph_pasos = None
        self.pasos_last = last_id
        self.last_edit = 0.0
        self.last_poll_pasos = 0.0
        self.cot_full = ""    # CoT pendiente de mandar (lo enviado se LIBERA)
        # F42 (Damian: el CoT del CELU también en botones): cada fase de
        # pensado que CIERRA es un bloque → botón. cot_full NO se libera
        # en modo botones (_cot_flush no-op): los cortes por índice de
        # bloque necesitan el texto entero e íntegro.
        self.cot_cajas = []      # F43: cajas SELLADAS por tamaño
        self.cot_rotulos = []    # F46: «CoT R.C» — razonada R, caja C
        self.cot_sell = cotrz.SelladorCot()  # sella al tope, reinicia por run
        self.cot_ph = None      # F46: la caja de botones de la razonada EN CURSO
        self.cot_ph_ultimo = None  # …y la última nacida (el final la edita)
        self.cot_phs = []       # F46: message_id por caja (el tap edita SU mensaje)
        self.cot_razonada = 1   # F46: razonada en curso (para el rótulo R.C)
        self.cot_raz_ini = 1    # índice global de su primera caja
        self.cot_raz_n = 0      # cajas selladas de la razonada en curso
        self.cot_flushed = 0  # chars ya enviados como mensajes permanentes
        # mensajes de CoT PUBLICADOS: trozos de texto en modo stream, cajas
        # de botones nacidas en modo botones (los edits no cuentan)
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
        if not self.cfg.get("cot_stream") or self.cfg.get("cot_botones"):
            return  # off, o F42: en modo botones el texto no se streamea
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

    # ------------------------------------------------- F42: CoT en botones

    def _cot_guardar(self, **extra):
        """El cache del turno, SIEMPRE con el ph de la caja de botones si ya
        nació (F43c: el tap edita ese mensaje — una reescritura sin ph lo
        perdía y el tap caía abajo) y con los phs POR CAJA (F46: cada
        razonada tiene su mensaje; el tap edita el de SU razonada)."""
        ph = self.cot_ph or self.cot_ph_ultimo
        if ph:
            extra["ph"] = ph
        extra["phs"] = list(self.cot_phs)
        try:
            cotrz.guardar(self.sid, int(self.t0), self.titulo,
                          self.cot_cajas, self.cot_rotulos, **extra)
        except OSError:
            pass

    @property
    def _cot_activo(self):
        """R4 — los caminos de cajas de botones solo corren con el knob
        prendido y SIN haber pasado a segundo plano (F15): el guard que
        se repetía en cada entrada."""
        return bool(self.cfg.get("cot_botones")) and not self.segundo_plano

    def _cot_caja_nueva(self, caja, rotulo):
        """F43: una caja se LLENÓ (o cerró la razonada y la cola se sella):
        botón nuevo YA — las cajas aparecen a medida que se crean. F46: la
        caja de botones es POR RAZONADA — nace con la primera caja sellada
        de cada razonada, en su posición cronológica (entre las tools de
        esa fase), y crece solo mientras ella vive; la próxima razonada
        nace en un mensaje NUEVO. Hace falta su message_id (send lo
        devuelve; con un True acá cada edit fallaba y la caja quedaba
        clavada — lo que vio Damian)."""
        if not self._cot_activo:
            return
        self.cot_cajas.append(caja)
        self.cot_rotulos.append(rotulo)
        self.cot_raz_n += 1
        self._cot_guardar(vivo=True)  # antes del botón: el tap ya la encuentra
        pend, k = self.cot_sell.en_curso
        texto = cotrz.texto_vivo(
            self.titulo, self.cot_raz_n,
            "%d.%d" % (self.cot_razonada, k), pend)
        kb = {"inline_keyboard": cotrz.botones_cajas(
            self.sid, int(self.t0), self.cot_rotulos[self.cot_raz_ini - 1:],
            ini=self.cot_raz_ini)}
        if self.cot_ph:
            if self.live_edit(self.cfg, self.cot_ph, texto, markup=kb):
                cotrz.log_teclado("celu viva r%d" % self.cot_razonada,
                                  self.cot_ph, kb)
        else:
            self.cot_ph = cotrz.ph_de(
                self.send(self.cfg, texto, loud=False, markup=kb))
            if self.cot_ph:
                self.cot_ph_ultimo = self.cot_ph
                self.cot_nmsg += 1
                cotrz.log_teclado("celu nace r%d" % self.cot_razonada,
                                  self.cot_ph, kb)
        self.cot_phs.append(self.cot_ph)
        if self.cot_ph:
            self._cot_guardar(vivo=True)  # ph + phs al día para el tap

    def _cot_run_cerrar(self):
        """Terminó una razonada (pasa a escribir / llama una tool): la cola
        se sella como última caja y SU caja de botones queda con estado
        final propio; la próxima razonada nace en un mensaje NUEVO (F46,
        idea de Damian: cada tanda de cajitas en el momento del turno en
        que ocurrió, no todo acumulado arriba)."""
        if not self._cot_activo:
            return
        r = self.cot_razonada
        for caja, n in self.cot_sell.cerrar():
            self._cot_caja_nueva(caja, "CoT %d.%d" % (r, n))
        if not self.cot_raz_n:
            return  # F48c: razonada sin cajas — no consume número ni mensaje
        if self.cot_ph:
            ini = self.cot_raz_ini
            fin = ini + self.cot_raz_n
            # R9: la fórmula del cierre vive en razonamiento (la comparte
            # el vigilante de la Mac); el transporte es el de acá
            texto, kb = cotrz.cierre_razonada(
                self.sid, int(self.t0), self.titulo, r,
                self.cot_cajas, self.cot_rotulos, ini, fin)
            if self.live_edit(self.cfg, self.cot_ph, texto, markup=kb):
                cotrz.log_teclado("celu cierre r%d" % r, self.cot_ph, kb)
        self.cot_razonada = r + 1
        self.cot_ph = None       # la próxima razonada nace en mensaje nuevo
        self.cot_raz_ini += self.cot_raz_n
        self.cot_raz_n = 0

    def _cot_vacio(self):
        """F45 — ¿cerró SIN razonamiento y hay que decirlo? Solo si el stream
        se vio entero (hubo deltas y sin trip anti-OOM) y nadie frenó el
        turno: sin deltas es un turno de la app de la Mac (su stream no
        cruza al bot) y con /stop el silencio es del usuario."""
        return bool(self.cfg.get("cot_aviso_vacio")) and self.n_deltas > 0 \
            and self.razon_n == 0 and not self.events_off \
            and not self.stop_pedidos

    # ------------------------------------------------- fase eventos

    def _poll_events(self):
        if self.events_off:
            # trip activado: no volver a pedir el replay gigante — los
            # pasos y el documento final cubren el resto del turno
            return
        params = {"sessionId": self.sid}
        if self.last_seq is not None:
            # F7b: poll INCREMENTAL — el server solo devuelve seq > afterSeq
            # (spike F7a: verificado, 0 eventos tras el cursor). El dedupe
            # por eventId queda como red de seguridad ante un replay.
            params["afterSeq"] = self.last_seq
        r = self.c.request("session/events", params, timeout=20)
        evs = (r.get("result") or {}).get("events") or []
        tam = _frame_size(r)
        es_primer = self.primer_poll
        if self.primer_poll:
            self.primer_poll = False
            self.cad = self.poll_cadence(tam)
            self.pwlog("live evs=%d frame=%.1fMB rss=%dMB rss_now=%dMB cad=%.0fs"
                       % (len(evs), tam // 1048576, self.rss(),
                          self.rss_now(), self.cad))
            # F7+++: el PRIMER poll puede traer el replay completo UNA vez
            # (sin cursor): se parsea, se muestra y se libera — NO tripear
            # (Damian: el CoT se quiere ver SIEMPRE entero; el peligro era
            # el replay CADA 2 s del régimen pre-afterSeq, no una vez).
        if not es_primer:
            if tam > LIVE_FRAME_TRIP:
                # un frame GIGANTE en un poll incremental = el cursor no
                # está funcionando (el server re-replaya): esto sí es el
                # régimen que inflaba la RSS — cortar y marcar la sesión
                self.events_off = True
                self.skip_mark(self.sid)
                self.pwlog("live events OFF: frame de %.1fMB en poll "
                           "INCREMENTAL (afterSeq roto) — flag %dh por sesión"
                           % (tam // 1048576, LIVE_SKIP_TTL // 3600))
            elif len(evs) > EVENTS_TRIP:
                self.events_off = True
                self.pwlog("live events OFF: replay %d evs (anti-OOM) — "
                           "sigo con pasos+documento" % len(evs))
            if self.events_off:
                return
        # F7b: el cursor avanza con el seq máximo VISTO (aunque el evento
        # se filtre abajo por tamaño/dedupe)
        for ev in evs:
            sq = ev.get("seq")
            if isinstance(sq, int) and (self.last_seq is None
                                        or sq > self.last_seq):
                self.last_seq = sq
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
                for _caja, _n in self.cot_sell.alimentar(dd):
                    # F43: al tope; F46: rótulo R.C de la razonada en curso
                    self._cot_caja_nueva(_caja,
                                         "CoT %d.%d" % (self.cot_razonada, _n))
            elif k == "text_delta" and p.get("delta"):
                dd = p["delta"]
                self.texto = (self.texto + dd)[-8192:]  # cola para la caja
                self.texto_n += len(dd)                  # viva; el documento
                self.n_deltas += 1                       # final sale del store
                if self.razon:
                    # EVENTO DE FASE: pasó de pensar a escribir. En modo
                    # botones (F42) el bloque se cierra y la caja de botones
                    # ya lo cuenta — sin mensaje de fase extra. F48c: CADA
                    # razonada cierra acá (antes solo la primera: el guard
                    # evento_write no se reseteaba nunca → la cola de la 2ª
                    # en adelante quedaba en el sellador y la razonada
                    # siguiente se le FUSIONABA bajo el mismo R)
                    self._cot_run_cerrar()
                    if not self.evento_write:
                        # el aviso de fase del modo texto: UNA vez por turno
                        self.evento_write = True
                        if self.cfg.get("cot_stream") \
                                and not self.cfg.get("cot_botones") \
                                and not self.segundo_plano:
                            self.send(self.cfg,
                                      "🧠 <b>«%s»</b> — análisis terminado "
                                      "(%s chars de CoT · %d cajas)\n✍️ ahora "
                                      "escribiendo la respuesta…"
                                      % (esc(self.titulo),
                                         fmt_tok(len(self.cot_full)),
                                         self.cot_nmsg), loud=False)
                self.razon = ""  # empezó a escribir: el CoT ya cumplió
            elif k in ("tool_input_start", "tool_input_delta",
                       "tool_input_end", "tool_call",
                       "started", "result", "batch"):
                if k == "tool_input_start" and self.razon:
                    self._cot_run_cerrar()  # F43: pensó → va a llamar
                    # F48c: la fase de pensado terminó — las tools que sigan
                    # sin pensar nuevo no re-cierran (cada re-cierre vacío
                    # consumía un número R: «CoT 34.3» con 19 razonadas)
                    self.razon = ""
                # F7c: ciclo de vida de la tool por el stream (spike F7a:
                # tool_input_start→deltas→tool_call{input ya parseado}→
                # tool.updated.started→tool.updated.result{success,content})
                self._tool_evento(k, p)

    # ------------------------------------------- ciclo de vida de tools

    def _tool_evento(self, k, p):
        """F7c — actualiza tools_vivo según el evento del stream. Las cajas
        permanentes nacen 🔵 al completar el input (tool_call trae el input
        YA parseado) y se EDITAN a ✅/❌ con tool.updated.result."""
        if not self.cfg.get("tools_stream"):
            return
        tc = p.get("toolCallId")
        if not tc or k in ("started", "batch", "tool_input_end"):
            return  # started/batch no agregan nada visible; end es implícito
        e = self.tools_vivo.get(tc)
        if e is None:
            e = self.tools_vivo[tc] = {"nombre": "tool", "texto": "",
                                       "inp": None, "msg_id": None,
                                       "fin": None, "salida": ""}
        if k == "tool_input_start":
            e["nombre"] = str(p.get("toolName") or e["nombre"])
        elif k == "tool_input_delta" and p.get("delta"):
            # el input llega tipeándose (JSON crudo): cola para la caja viva
            e["texto"] = (e["texto"] + p["delta"])[-4000:]
        elif k == "tool_call":
            e["nombre"] = str(p.get("toolName") or e["nombre"])
            if isinstance(p.get("input"), dict):
                e["inp"] = p["input"]
            # índice de pasos al instante (no esperar al poll de mensajes)
            if e["inp"] is not None:
                self.pasos.append(ToolStep(
                    e["nombre"], _tool_desc_corto(e["nombre"], e["inp"])))
                if not self.segundo_plano \
                        and _tool_caja_on(e["nombre"], self.cfg):
                    self.pasos_nmsg[0] += 1
                    try:
                        e["msg_id"] = self.live_send(
                            self.cfg, "%s · <b>%d</b>"
                            % (self._texto_caja(e), self.pasos_nmsg[0]))
                    except Exception:  # noqa: BLE001
                        pass
                    if self.pasos_nmsg[0] % 3 == 0:
                        self.sleep(1.0)  # pace ~1 msg/s del chat
        elif k == "result":
            res = p.get("result") or {}
            e["fin"] = "✅" if res.get("success") else "❌"
            e["salida"] = str(res.get("content") or "")[:800]
            if e.get("msg_id") and not self.segundo_plano:
                try:
                    self.live_edit(self.cfg, e["msg_id"], self._texto_caja(e))
                except Exception:  # noqa: BLE001
                    pass
        # cap anti-crecimiento: conservar las últimas 60 (las cerradas
        # primero — su caja ya vive en el chat, no hace falta el estado)
        if len(self.tools_vivo) > 60:
            cerradas = [t for t, d in self.tools_vivo.items() if d.get("fin")]
            for t in cerradas[:len(self.tools_vivo) - 60]:
                del self.tools_vivo[t]

    def _texto_caja(self, e):
        """Texto de la caja de la tool con su marca de estado (🔵 corriendo
        / ✅ / ❌) y la salida cuando la hubo."""
        base = _tool_caja(e["nombre"], dict(e.get("inp") or {}))
        base = base.replace("</b>", "</b> %s" % (e.get("fin") or "🔵"), 1)
        if e.get("salida") and "└" not in base:
            base += "\n└ <pre>%s</pre>" % esc(e["salida"])
        return base

    def _linea_tool_viva(self):
        """Línea de la tool corriente para la caja viva: el comando
        Tipeándose (input_streaming) o 🔵 corriendo — el "live action"."""
        if not self.cfg.get("tools_stream"):
            return ""
        for _tc, e in reversed(list(self.tools_vivo.items())):
            if e.get("fin"):
                continue
            if e.get("inp") is not None:
                d = _tool_desc_corto(e["nombre"], e["inp"])
                return ("\n🔧 <b>%s</b> 🔵 corriendo — <code>%s</code>"
                        % (esc(e["nombre"]), esc(d[:150])))
            if e["texto"]:
                return ("\n🔧 <b>%s</b> tipeando el comando:\n<pre>%s</pre>"
                        % (esc(e["nombre"]), esc(e["texto"][-220:])))
            return "\n🔧 <b>%s</b>…" % esc(e["nombre"])
        return ""

    # ------------------------------------------------- fase pasos

    def _poll_pasos(self):
        self.last_poll_pasos = self.now()
        try:
            nuevos = self.fetch_messages(self.c, self.sid,
                                         after_id=self.pasos_last, limit=30)
            if nuevos:
                self.pasos_last = _last_id_of(nuevos) or self.pasos_last
                con_id = _tools_con_call_id(nuevos)
                # F7c: las tools ya sumadas al índice por el stream
                # (tool_call) no se duplican cuando el historial las trae
                self.pasos.extend(
                    ToolStep(n, _tool_desc_corto(n, i))
                    for cid, n, i, _st in con_id
                    if not (cid and cid in self.tools_vivo))
                # F7c: cerrar cajas 🔵 cuyo result del stream se perdió
                # (events_off a mitad de tool) — el historial trae la salida
                for cid, nombre, inp, status in con_id:
                    e = self.tools_vivo.get(cid)
                    if not (e and e.get("msg_id") and not e.get("fin")):
                        continue
                    e["fin"] = "❌" if str(status) == "error" else "✅"
                    sal = inp.get("output") or inp.get("result") or ""
                    if isinstance(sal, str) and sal.strip():
                        e["salida"] = sal[:800]
                    try:
                        self.live_edit(self.cfg, e["msg_id"],
                                       self._texto_caja(e))
                    except Exception:  # noqa: BLE001
                        pass
                # F22bis: cajas PERMANENTES de cada tool call nuevo (comando
                # completo / diff) — `nuevos` ya es incremental (after_id);
                # boxed = las que ya nacieron 🔵 del stream
                if not self.segundo_plano:
                    try:
                        self.cajas_paso(self.cfg, self.titulo,
                                        self.pasos_nmsg, con_id,
                                        boxed={t for t, d in
                                               self.tools_vivo.items()
                                               if d.get("msg_id")})
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------- caja viva

    def _cuerpo_caja_viva(self, trans):
        """R10 — el TEXTO de la caja viva según la fase del turno (puro:
        estado → string, sin I/O; _edit_live_box lo publica). Ramas:
        escribiendo, stream pausado por anti-OOM, pensando (estado con
        botones F43b / texto crudo con cot_stream), turno de la Mac (sin
        deltas: su streaming no cruza) y pensando a secas."""
        if self.texto:
            return ("⚙️ <b>«%s»</b> · %ds\n<pre>%s</pre>\n✍️ escribiendo…"
                    % (esc(self.titulo), trans, esc(self.texto[-3200:])))
        if self.events_off:
            return ("📄 <b>«%s»</b> · %ds\nCoT en vivo pausado "
                    "(el stream incremental falló — protección de "
                    "memoria; /compact lo reactiva).\n"
                    "🔧 pasos activos · el documento final trae el "
                    "CoT COMPLETO."
                    % (esc(self.titulo), trans))
        if self.razon:
            if self.cfg.get("cot_botones"):
                # F43b (Damian: "además del botón se ve el CoT"): con
                # botones el contenido vive SOLO en las cajas — acá va el
                # estado, no el texto crudo duplicado
                pend, k = self.cot_sell.en_curso
                return ("🧠 <b>«%s»</b> · %ds\npensando… · %d caja%s · "
                        "llenando la CoT %d.%d (%d chars)"
                        % (esc(self.titulo), trans, len(self.cot_cajas),
                           "s" if len(self.cot_cajas) != 1 else "",
                           self.cot_razonada, k, pend))
            return ("🧠 <b>«%s»</b> · %ds\n<b>pensando:</b>\n<pre>%s</pre>"
                    % (esc(self.titulo), trans, esc(self.razon[-3200:])))
        if self.n_deltas == 0 and trans > 12:
            # sin deltas = el turno lo ejecuta la app de la Mac (task
            # abierta ahí): su streaming interno no cruza al bot
            return ("🖥️ <b>«%s»</b> · %ds\ncorriendo en la Mac "
                    "(task abierta en la app) — te aviso al terminar"
                    % (esc(self.titulo), trans))
        return "🧠 <b>«%s»</b> · %ds\npensando…" % (esc(self.titulo), trans)

    def _edit_live_box(self):
        self.last_edit = self.now()
        # cajas PERMANENTES de CoT: lo que ya no cabe en la cola viva se
        # manda como mensaje fijo (scrolleable de principio a fin)
        self._cot_flush(COT_TAIL)
        trans = int(self.now() - self.t0)
        # F7c: la tool corriente, al lado del CoT — el comando tipeándose
        # en vivo o 🔵 corriendo
        cuerpo = self._cuerpo_caja_viva(trans) + self._linea_tool_viva()
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
        self._cierre()
        # [1] es la COLA del texto (8KB) — el texto íntegro sale del historial
        return self.cot_full, self.texto, self.pasos, self.live_id

    def _cierre(self):
        """R10 — el cierre del turno (las 32 líneas que cerraban run, la
        parte más densa y la menos testeada directo): la lluvia final de
        cajas de texto, el ÍNDICE de botones del CoT (o el aviso «sin
        CoT» de F45) y la telemetría del pwlog."""
        # fin del turno: el CoT restante también a mensajes permanentes —
        # el chat queda con el razonamiento COMPLETO scrolleable
        if not self.segundo_plano:  # F15: backgrounded = sin lluvia de cierre
            try:
                self._cot_flush(0, pace=False)
            except Exception:  # noqa: BLE001
                pass
            if self.cfg.get("cot_botones"):
                # F43: la cola de la razonada en vuelo se sella y ESA
                # razonada cierra con su mensaje y sus botones, como todas.
                # F49: el turno NO deja un índice global al final — antes el
                # mensaje de la última razonada se editaba con TODOS los
                # botones del turno (Damian: «en la última caja se
                # acumularon todas, está andando re mal»). Solo si alguna
                # caja quedó sin mensaje (send fallido) nace UN mensaje de
                # tardías con ESAS: ningún botón se pierde, nada se acumula.
                self._cot_run_cerrar()
                if self.cot_cajas:
                    sin = [i + 1 for i, p in enumerate(self.cot_phs) if not p]
                    if sin:
                        kb = {"inline_keyboard": cotrz.botones_indices(
                            self.sid, int(self.t0), self.cot_rotulos, sin)}
                        nph = cotrz.ph_de(self.send(
                            self.cfg, cotrz.texto_rezagadas(self.titulo,
                                                            len(sin)),
                            loud=False, markup=kb))
                        if nph:
                            self.cot_nmsg += 1
                            for i in sin:
                                self.cot_phs[i - 1] = nph
                            cotrz.log_teclado("celu tardias", nph, kb)
                    self._cot_guardar(finalizado=True)
                elif self._cot_vacio():
                    # F45: el stream corrió entero y ni un reasoning_delta —
                    # una línea que lo diga (antes: silencio = ¿bug?)
                    self.send(self.cfg, cotrz.texto_vacio(self.titulo,
                                                          int(self.t0)),
                              loud=False)
        self.pwlog("live fin: deltas=%d razon=%d texto=%d pasos=%d cot_msgs=%d "
                   "cajas=%d"
                   % (self.n_deltas, self.razon_n, self.texto_n,
                      len(self.pasos), self.cot_nmsg, len(self.cot_cajas)))
