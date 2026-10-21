"""razonamiento — CoT in SIZE-based BOXES, with buttons (F40-F44).

Damian's model (F43, verbatim): "a box has a size; when the CoT reaches
the cap the box is full and is sent with its number; the CoT continues
and brakes at the cap again, and so buttons arrive incrementally until
the CoT ends; then the chat writes or sends a form; and when it thinks
again, it sends a numbered CoT again starting from 1". That is: BOXES
SEALED BY SIZE during the reasoning pass (not by phase), a button per
box as soon as it fills, and numbering that RESTARTS on every reasoning
pass.

Sources (all three write the same cache and use the same buttons):
- F42 phone: LiveTurnView feeds a SelladorCot with the reasoning_delta.
- F41 Mac: `--cot-vivo` polls the history (the reasoning part of the step
  in progress is already readable mid-step — verified live 20/10).
  F44: the vigilante stays WARM between turns of the same session — ONE
  resume per session, not per turn (bot_development's resume took ~45s
  and the first little box arrived at ~48s into EVERY prompt). The
  pid-file cot-vivo-<sid>.pid (pid + heartbeat in the mtime) is the
  hook's single-flight: with a live, fresh vigilante no other is spawned.
- F40 Mac (close-time fallback): `--cot-botones` captures post-turn.

The tap (ct:sid:t0:n[:p] → /cotb) reads the cache: no runtime, instant.
COT_CAJA_CAP = COT_PARTE_CAP → each box opens WHOLE in one message, no
part sub-buttons. The cache stores `ph` (the message_id of the button
box) so the tap edits IN the message (F43c): send returns that id and
every cache rewrite preserves it.

F45: a turn that closes WITHOUT reasoning says so in one line
(texto_vacio) instead of staying silent — all three sources. Diagnosis
from 20/10 over the runtime's raw rollout: the bot requests IDENTICAL
thinking for GLM-5.3 and Flash (thinking enabled + effort max); GLM-5.3
reasons ADAPTIVELY — it decides per call — and in the bot's session it
went 12 calls in a row without reasoning at ~130k tokens of context (it
came back after compacting); Flash reasons almost always. Without the
notice, zero little boxes was indistinguishable from a bug.
"""
import datetime
import json
import os
import re
import time

from zcode_bot import config
from zcode_bot.config import marker_path
from zcode_bot.core import _stop_requested, _task_current_ok, _worker_owned
from zcode_bot import proto as zc_proto
from zcode_bot.messages import _fetch_messages, _last_id_of
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, partes_iguales, task_label
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import pwlog

COT_PARTE_CAP = 3000      # chars per part when OPENING an old/large box
COT_CAJA_CAP = COT_PARTE_CAP  # chars per BOX: at the cap it is sealed and its button travels
COT_BOTONES_MAX = 20      # keyboard: more boxes → /cot note (document)
COT_BOTONES_FILA = 5
COT_MSGS = 60             # messages to fetch: the longest Mac turn fits
COT_RESUME_TIMEOUT_S = 120
ARCH_COT = "cot-%s-%d.json"
ARCH_COT_PID = "cot-vivo-%s.pid"  # F44: pid of the session's warm vigilante


def cache_path(sid, t0):
    return os.path.join(config.STATE_DIR, ARCH_COT % (sid, int(t0)))


def ph_de(res):
    """Usable message_id from what uhttp.send returned: True (sent but
    without an id) is no good for editing — and bool IS a subclass of int."""
    return res if isinstance(res, int) and not isinstance(res, bool) else None


def _con_ph(ph):
    return {"ph": ph} if ph else {}


# ------------------------------------------------------------- the sealer

class SelladorCot:
    """Seals boxes by SIZE within ONE reasoning pass: alimentar() returns
    the boxes that filled (with their local number, from 1); cerrar()
    seals the tail as the pass's last box and RESETS the state for the
    next one. The caller builds the VISIBLE label: «CoT R.C» (F46) —
    reasoning pass R, box C within it — so there are never two «CoT 1».
    No odd states: the accumulated text is kept whole until the pass
    closes."""

    def __init__(self, cap=COT_CAJA_CAP):
        self.cap = cap
        self.texto = ""
        self.selladas = 0

    def alimentar(self, delta):
        """[(box, n_local)] of the boxes that reached the cap."""
        self.texto += delta
        nuevas = []
        while len(self.texto) >= self.cap * (self.selladas + 1):
            self.selladas += 1
            corte = self.cap * self.selladas
            nuevas.append((self.texto[corte - self.cap:corte],
                           self.selladas))
        return nuevas

    def cerrar(self):
        """End of the reasoning pass: the tail (if it has anything) is
        the last box. State resets for the next pass."""
        cola = self.texto[self.cap * self.selladas:]
        n = self.selladas + 1
        self.texto, self.selladas = "", 0
        return [(cola, n)] if cola.strip() else []

    @property
    def en_curso(self):
        """(chars of the box currently filling, that box's n_local)."""
        resto = len(self.texto) - self.cap * self.selladas
        return max(0, resto), self.selladas + 1


def cajas_de_texto(texto, cap=COT_CAJA_CAP):
    """Boxes of an ALREADY finished reasoning pass: filled to the cap +
    final tail. [(box, n_local)] with numbering from 1."""
    return [(texto[i:i + cap], i // cap + 1)
            for i in range(0, len(texto), cap)] if texto.strip() else []


# ------------------------------------------- from the history (Mac: F40/F41)

def _partes_del_turno(msgs):
    """The turn's reasoning parts (after the last user) GROUPED into
    reasoning passes: a pass ends when the model DOES something (a
    tool/text part between reasonings); contiguous reasonings are the
    same pass. [(razonada_texto, en_curso_bool)] — the last one may keep
    growing (it is the last message's last part)."""
    ini = 0
    for i, m in enumerate(msgs):
        rol = (m.get("info") or {}).get("role") or m.get("role")
        if rol == "user":
            ini = i + 1
    razonadas, actual, en_curso = [], [], False
    for mi, m in enumerate(msgs[ini:]):
        parts = m.get("parts") or []
        for pi, p in enumerate(parts):
            if not isinstance(p, dict):
                continue
            tp = p.get("type")
            es_ultima_part = (mi == len(msgs[ini:]) - 1
                              and pi == len(parts) - 1)
            if tp == "reasoning":
                t = str(p.get("text") or p.get("reasoning") or "")
                if t.strip():
                    actual.append(t)
                    en_curso = es_ultima_part
            elif tp in ("tool", "text") and actual:
                # the model did something between reasonings: the pass closed
                razonadas.append("".join(actual))
                actual, en_curso = [], False
    if actual:
        razonadas.append("".join(actual))
    return razonadas, en_curso


def _cajas_de_razonadas(razonadas, hasta=None, base_r=0):
    """(cajas, rotulos) of the given reasoning passes — the loop that
    cajas_del_turno and cajas_vivas were each duplicating. hasta = only
    the first N (the in-progress one stays out); base_r = initial
    numbering (the cajas_vivas tail keeps the LAST pass's numbering)."""
    cajas, rotulos = [], []
    trozadas = razonadas[:hasta] if hasta is not None else razonadas
    for r, texto in enumerate(trozadas, base_r + 1):
        for caja, n in cajas_de_texto(texto):
            cajas.append(caja)
            rotulos.append("CoT %d.%d" % (r, n))
    return cajas, rotulos


def cajas_del_turno(msgs):
    """COMPLETE boxes of the turn (turn already closed): each reasoning
    pass fills its boxes to the cap + a tail. F46: label «CoT R.C»
    (pass R, box C) — unique, never two «CoT 1». → parallel
    (cajas, rotulos)."""
    razonadas, _en_curso = _partes_del_turno(msgs)
    return _cajas_de_razonadas(razonadas)


def cajas_vivas(msgs):
    """Same as cajas_del_turno but for the turn IN PROGRESS: the pass
    under way only contributes the boxes ALREADY FILLED (not the one
    filling now). → (cajas, rotulos, chars_en_curso, rotulo_en_curso
    «R.C»)."""
    razonadas, en_curso = _partes_del_turno(msgs)
    cajas, rotulos = _cajas_de_razonadas(
        razonadas, hasta=len(razonadas) - 1 if en_curso else None)
    if en_curso and razonadas:
        r = len(razonadas)
        texto = razonadas[-1]
        llenas = len(texto) // COT_CAJA_CAP
        mas_cajas, mas_rotulos = _cajas_de_razonadas(
            [texto[:llenas * COT_CAJA_CAP]], base_r=r - 1)
        cajas += mas_cajas
        rotulos += mas_rotulos
        pend = len(texto) % COT_CAJA_CAP
        return cajas, rotulos, pend, "%d.%d" % (r, llenas + 1)
    return cajas, rotulos, 0, ""


# ------------------------------------------------------------- buttons

def _filas(botones):
    filas, fila = [], []
    for b in botones:
        fila.append(b)
        if len(fila) == COT_BOTONES_FILA:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    return filas


def botones_cajas(sid, t0, rotulos, ini=1):
    """One button per box; labels «CoT R.C» (F46: never two «CoT 1») and
    the callback_data carries the GLOBAL index: ct:sid:t0:n. ini = the
    first label's global index (F46: ONE pass's keyboard is a slice of
    the total)."""
    return _filas([{"text": rotulos[i - ini][:36],
                    "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                   for i in range(ini,
                                  ini + min(len(rotulos), COT_BOTONES_MAX))])


def botones_partes(sid, t0, n, total, actual):
    return _filas([{"text": ("[%d/%d]" if p == actual else "%d/%d")
                    % (p, total),
                    "callback_data": "ct:%s:%d:%d:%d" % (sid, int(t0), n, p)}
                   for p in range(1, total + 1)])


def botones_indices(sid, t0, rotulos, idxs):
    """F49 — buttons for arbitrary 1-based GLOBAL indexes (the boxes that
    live in ONE message: the pass's own, or the late ones from the
    close); rotulos is the turn's complete list."""
    return _filas([{"text": rotulos[i - 1][:36],
                    "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                   for i in idxs[:COT_BOTONES_MAX]])


def log_teclado(origen, ph, kb):
    """F49 — the feedback that was missing (Damian: «you have no way to
    get feedback»): every send/edit of a button box records in the pwlog
    WHICH buttons that message ended up showing. So the chat can be
    rebuilt from the log without looking at it."""
    etiquetas = [b.get("text", "?") for fila in (kb or {}).get(
        "inline_keyboard", []) for b in fila]
    pwlog("cot kb %s ph=%s [%d]: %s" % (origen, ph, len(etiquetas),
                                        " ".join(etiquetas)))


# ------------------------------------------------------------- cache

def guardar(sid, t0, titulo, cajas, rotulos, **extra):
    """Writes/updates a turn's cache (all three sources). extra:
    vivo/finalizado. 'bloques' is kept for compatibility with old caches
    (mostrar accepts it). tmp+replace (audit 20/10): the "w" truncated
    the file in front of ANOTHER process (the Mac's vigilante writes
    every ~3s while the poller reads on every tap → FALSE «no longer
    saved») and a kill -9 mid-dump left the cache truncated FOREVER —
    the turn lost its buttons for life."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = "%s.tmp.%d" % (cache_path(sid, t0), os.getpid())
    with open(tmp, "w") as f:
        json.dump({"sid": sid, "titulo": titulo, "t0": int(t0),
                   "cajas": cajas, "rotulos": rotulos, **extra}, f)
    os.replace(tmp, cache_path(sid, t0))


def cache_existe(sid, t0):
    return os.path.exists(cache_path(sid, t0))


def cache_cerrada_o_vivo(sid, t0):
    """F51a — did someone publish, or is someone publishing, this turn's
    close? Yes if the cache ended up finalizado (the live one closed) or
    there is a vigilante PROCESS (pid alive; freshness infinite: while
    resuming it can carry an expired heartbeat for up to 120s and still
    be the owner — possession is marked by the vivo=True cache it wrote,
    F41). A vivo=True cache with a DEAD pid is a live one that died
    midway: before, that still suppressed the fallback and the turn
    closed WITHOUT boxes — with the cache lying alive forever."""
    try:
        with open(cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict):
        return False
    return bool(d.get("finalizado")) \
        or vigilante_activo(sid, fresco_s=float("inf"))


def cerrar_cache(sid, t0, **extra):
    """F47d — closes the cache of a turn that died WITHOUT its close
    (worker killed off by /stop, kill -9, OOM): the LiveTurnView's last
    guardar was vivo=True and that is how it stayed forever. Keeps
    cajas/rótulos/ph/phs — only removes vivo, marks finalizado and adds
    extra (e.g. error=). False if there is no cache (turn without boxes)
    or it could not be read/written."""
    try:
        with open(cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict):
        return False
    d.pop("vivo", None)
    d["finalizado"] = True
    d.update(extra)
    try:
        tmp = "%s.tmp.%d" % (cache_path(sid, t0), os.getpid())
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, cache_path(sid, t0))
    except OSError:
        return False
    return True


def texto_vivo(titulo, n, en_curso="", pend=0):
    """Status of the button box: sealed boxes + the one filling
    (en_curso = «R.C» label of the box being filled)."""
    linea = "🧠 <b>«%s»</b> pensando… · %d caja%s" % (
        esc(titulo or "?"), n, "s" if n != 1 else "")
    if pend:
        linea += " · llenando la CoT %s (%d chars)" % (en_curso, pend)
    if n:
        linea += "\nTocá un botón para leerlo:"
    return linea


def texto_razonada(titulo, r, n, total):
    """F46 — close of ONE reasoning pass: its button box keeps its own
    final status (the turn goes on: the next pass is born in a new
    message, in its chronological position)."""
    return ("🧠 <b>«%s»</b> · razonada %d — %d caja%s, %d chars. "
            "Tocá una para leerla:"
            % (esc(titulo or "?"), r, n, "s" if n != 1 else "", total))


def texto_final(titulo, t0, n, total):
    hora = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    texto = ("🧠 <b>Razonamiento</b> de «%s» (turno de las %s) — %d caja%s, "
             "%d chars. Tocá una para leerla:"
             % (esc(titulo or "?"), hora, n, "s" if n != 1 else "", total))
    if n > COT_BOTONES_MAX:
        texto += ("\n(botones para las primeras %d; el documento completo "
                  "con /cot)" % COT_BOTONES_MAX)
    return texto


def texto_rezagadas(titulo, n, t0):
    """F49 — boxes whose reasoning pass never got its own message (send
    failed on the phone; passes born in the Mac's close-time fetch): ONE
    message at the end with THOSE — no button is lost and nothing piles
    up (the old global index: «they all piled up in the last box»).
    F51a: 📥 + «turn of HH:MM» — it lands at the close and without an
    anchor it read as another turn's activity (incident 21/10 08:18: the
    07:43 turn's boxes falling onto a just-started turn)."""
    hora = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    return ("📥 🧠 <b>«%s»</b> · %d caja%s al cierre del turno — turno de las "
            "%s. Tocá una para leerla:"
            % (esc(titulo or "?"), n, "s" if n != 1 else "", hora))


def texto_vacio(titulo, t0):
    """F45 — the turn closed and there was NOT ONE reasoning part/delta:
    one line to say so. Without this the chat stayed mute and zero little
    boxes was indistinguishable from a bug (Damian, 20/10: 'fix it so
    there is cot on both models' — the bot already requested thinking
    the same for both)."""
    hora = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    return ("🧠 <b>«%s»</b> (turno de las %s) — <b>sin CoT</b>: el modelo no "
            "razonó en este turno (thinking adaptativo: decide por llamada; "
            "GLM-5.3 suele saltearlo con el contexto muy largo — compactar "
            "lo trae de vuelta; Flash razona casi siempre)."
            % (esc(titulo or "?"), hora))


# ------------------------------------------------------------- sources

def capturar(cfg, sid, titulo, t0, ws):
    """F40 — fallback when a Mac turn closes (if the live one could not):
    fetches the history, caches the boxes and sends the message with
    buttons. Returns the box count: 0 if the turn closed without
    reasoning (F45: announced in one line), None if the resume failed or
    there was no history."""
    c = zc_proto.make(cwd=ws)
    try:
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
        if e := zc_proto.proto_err(r, "resume"):
            pwlog("cot-botones %s: %s" % (sid[-8:], e[:120]))
            return None
        msgs = _fetch_messages(c, sid, limit=COT_MSGS) or []
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    cajas, rotulos = cajas_del_turno(msgs)
    if not cajas:
        if not msgs:
            return None
        if cfg.get("cot_aviso_vacio"):
            # F51a: 📥 — this message arrives DELAYED (the fallback paid
            # for the history resume); the prefix tells it apart from the
            # vigilante's live close.
            uhttp.send(cfg, "📥 " + texto_vacio(titulo or sid[-8:], t0),
                       loud=False)
        return 0
    guardar(sid, t0, titulo, cajas, rotulos)
    total = sum(len(b) for b in cajas)
    ph = ph_de(uhttp.send(cfg, "📥 " + texto_final(titulo or sid[-8:], t0,
                                                    len(cajas), total),
                          loud=False,
                          markup={"inline_keyboard": botones_cajas(sid, t0,
                                                                   rotulos)}))
    if ph:
        guardar(sid, t0, titulo, cajas, rotulos, ph=ph)  # F43c
    return len(cajas)


def mostrar(sid, t0, n, parte=1):
    """The tap: box n from the cache (local label; global index on the button)."""
    try:
        with open(cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ("🤷 Ese razonamiento ya no está guardado (se purga a los "
                "2 días) — /cot trae el documento completo de la task.")
    cajas = d.get("cajas") or d.get("bloques") or []
    if n < 1 or n > len(cajas):
        return "⚠️ Caja fuera de rango (1 a %d)." % len(cajas)
    rotulos = d.get("rotulos") or []
    rotulo = rotulos[n - 1] if len(rotulos) >= n else "CoT %d" % n
    # (audit 20/10) escape BEFORE splitting: parts split by RAW cap
    # inflated up to 5x with esc() and send()'s truncation silently cut
    # the box — the box must open WHOLE (the part keyboard cannot lie
    # about what is on screen)
    partes = partes_iguales(esc(cajas[n - 1]), COT_PARTE_CAP)
    parte = min(max(parte, 1), len(partes))
    cab = "🧠 <b>%s</b> · «%s»" % (esc(rotulo),
                                   esc(d.get("titulo") or sid[-8:]))
    if len(partes) == 1:
        return cab + "\n<pre>%s</pre>" % partes[0]
    cab += " · parte %d/%d" % (parte, len(partes))
    return (cab + "\n<pre>%s</pre>" % partes[parte - 1],
            {"inline_keyboard": botones_partes(sid, t0, n, len(partes),
                                               parte)})


# ------------------------------------------------------------- F41: live Mac

COT_VIVO_CAD_S = 3.0      # cadence of the history polling
COT_VIVO_MAX_S = 35 * 60  # vigilante ceiling (the same as the spectator's)
COT_VIVO_WAIT_S = 1.0     # F44: cadence of the warm wait (small files, no runtime)
COT_VIVO_HB_FRESCO_S = 90  # F44: heartbeat older than this = hung vigilante (> the 60s
#                            session/messages timeout: a slow fetch is not a hang)
COT_VIVO_VACIO_POLLS = 5  # F44: consecutive polls with NOTHING after after_id (~15s) → re-resume
#                            (the new prompt must be there: if it is not seen, the
#                            connection is not seeing the store — a resume refreshes it)
CEDIDO = object()         # F44: a successor took the pid-file — this vigilante steps down
TECHO = object()          # F44: 35 min and the marker is still there — the same turn is not watched again


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", sid or "ses")


def pid_path(sid):
    return os.path.join(config.STATE_DIR, ARCH_COT_PID % _safe_sid(sid))


def pid_escribir(sid):
    """The vigilante registers itself: its pid in the file, its heartbeat
    in the mtime. The hook reads it to avoid spawning a second
    vigilante."""
    with open(pid_path(sid), "w") as f:
        f.write(str(os.getpid()))


def pid_latir(sid):
    try:
        os.utime(pid_path(sid), None)
    except OSError:
        pass


def pid_leer(sid):
    try:
        with open(pid_path(sid)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def pid_borrar(sid):
    """Only if the file is still MINE: a successor may have taken it over."""
    if pid_leer(sid) == os.getpid():
        try:
            os.remove(pid_path(sid))
        except OSError:
            pass


def _cedido(sid):
    """Another pid in the pid-file = a successor took the session (the
    hook saw the stale heartbeat). No file, no successor."""
    return pid_leer(sid) not in (0, os.getpid())


def vigilante_activo(sid, proc_vivo=None, now=None, fresco_s=COT_VIVO_HB_FRESCO_S):
    """F44 single-flight for the hook: is there a warm vigilante for this
    session? pid-file with a live pid and a fresh heartbeat. Dead or
    hung → False: the hook spawns another one, which takes the pid-file."""
    pid = pid_leer(sid)
    if not pid:
        return False
    try:
        edad = (now or time.time)() - os.path.getmtime(pid_path(sid))
    except OSError:
        return False
    if edad > fresco_s:
        return False
    if proc_vivo is None:
        from zcode_bot.workers import _proc_vivo  # lazy: workers → live_view → here
        proc_vivo = _proc_vivo
    return bool(proc_vivo(pid))


def vigilantes_pids():
    """pids from the cot-vivo-*.pid pid-files: /nuclear kills them off
    along with the workers (checking the cmdline: an orphaned file can
    point to a recycled pid)."""
    try:
        nombres = os.listdir(config.STATE_DIR)
    except OSError:
        return []
    pids = []
    for n in nombres:
        if not (n.startswith("cot-vivo-") and n.endswith(".pid")):
            continue
        try:
            with open(os.path.join(config.STATE_DIR, n)) as f:
                pid = int(f.read().strip() or 0)
        except (OSError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    return pids


def vigilantes_limpiar():
    try:
        nombres = os.listdir(config.STATE_DIR)
    except OSError:
        return
    for n in nombres:
        if n.startswith("cot-vivo-") and n.endswith(".pid"):
            try:
                os.remove(os.path.join(config.STATE_DIR, n))
            except OSError:
                pass


def _marker_estado(sid):
    """(mtime_int, token) of the in-flight turn's marker; None if there
    is no turn. token "" = marker mid-write (the hook truncates then
    writes): it resolves on the next poll, it is not a new turn."""
    p = marker_path(sid)
    try:
        mt = int(os.path.getmtime(p))
    except OSError:
        return None
    try:
        with open(p) as f:
            tok = str((json.load(f) or {}).get("token") or "")
    except (OSError, ValueError):
        tok = ""
    return mt, tok


def _otro_turno(ini, est):
    """Is the marker I see from a DIFFERENT turn than the one I started
    watching? By token (unique per prompt) when both were read; by mtime
    otherwise."""
    (mt0, tok0), (mt, tok) = ini, est
    if tok0 and tok:
        return tok != tok0
    return mt != mt0


def _raz_de(rotulo):
    """«CoT R.C» → R (0 if it does not parse: old «CoT N» labels with no dot)."""
    try:
        rc = str(rotulo).split()[1]
        return int(rc.split(".")[0]) if "." in rc else 0
    except (IndexError, ValueError):
        return 0


def _phs_de(rotulos, phs_map):
    """F46 — list of message_id per box (aligned to rotulos): the tap
    edits ITS pass's message; None where there was no message."""
    return [phs_map.get(_raz_de(rt)) for rt in rotulos]


def cierre_razonada(sid, t0, titulo, r, cajas, rotulos, ini, fin):
    """R9 — (text, keyboard) of ONE reasoning pass's close: its own final
    status with ITS buttons (global slice). The formula the phone
    (live_view._cot_run_cerrar) and the Mac's vigilante
    (_edit_fin_razonada) were duplicating — pure, each publishes it over
    its own transport. ini/fin = 1-based global indexes of its boxes."""
    return (texto_razonada(titulo, r, fin - ini,
                           sum(len(b) for b in cajas[ini - 1:fin - 1])),
            {"inline_keyboard": botones_cajas(sid, t0, rotulos[ini - 1:fin - 1],
                                              ini=ini)})


def _edit_fin_razonada(cfg, ph, sid, t0, titulo, r, cajas, rotulos, ini, fin):
    """F46 — close of ONE reasoning pass's button box (the Mac's
    vigilante): its own final status with ITS buttons; the next pass is
    born in a new message (chronological position: little boxes of phase
    1, tools/diffs, little boxes of phase 2…)."""
    texto, kb = cierre_razonada(sid, int(t0), titulo, r, cajas, rotulos,
                                ini, fin)
    try:
        if uhttp._live_edit(cfg, ph, texto, markup=kb):
            log_teclado("mac cierre r%d" % r, ph, kb)
    except Exception:  # noqa: BLE001 — the close must never break the vigilante
        pass


def _vigilar_turno(c, cfg, sid, titulo, t0, ini, after_id=None):
    """ONE Mac turn: polls the history every 3s until the marker
    disappears (on_stop: it closed), another one replaces it (a new
    prompt before we saw it close) or the 35 min ceiling. Each box that
    fills adds its button; the button box is BORN with the first one.

    after_id: the PREVIOUS turn's last message — this turn's is
    everything after it. In the warm pickup (F44) the new prompt may not
    be persisted on the first poll: without the cutoff, the history's
    last user would be the previous turn's and its boxes would travel
    again.

    → (corte, ultimo): corte None = closed cleanly; (mtime, token) of
    the foreign marker that cut it off; CEDIDO if another vigilante took
    the pid-file; TECHO if 35 min passed with the marker still there (it
    closes with whatever there is and is NOT watched again: an orphaned
    marker would be an eternal loop). ultimo = id of the last message
    seen (the next turn's after_id)."""
    visto, ph = -1, None
    phs_map = {}             # F46: pass → message_id of ITS button box
    r_actual, ph_ini = 0, 1  # pass on screen and its first global index
    vivas = ([], [])         # the latest shown live (monotonic guard)

    def _persistir(**extra):
        """R9 — the vigilante's guardar ALWAYS with up-to-date phs (the
        guardar+phs+_phs_de+_con_ph quartet that kept repeating)."""
        guardar(sid, t0, titulo, cajas, rotulos,
                phs=_phs_de(rotulos, phs_map), **_con_ph(ph), **extra)
    ultimo, corte, techo, vacios = after_id, None, False, 0
    t_fin = time.time() + COT_VIVO_MAX_S
    # F51c: a Mac turn that drags on ANNOUNCES itself — same value and
    # same phrase as the worker (R0b); post-box silence read as «it
    # froze». Next announcement in seconds (the _turn_end_poll pattern):
    # patchable in tests and free of granularity surprises.
    proximo_aviso = t0 + config.TURNO_LARGO_AVISO_S
    while True:
        if time.time() >= t_fin:
            techo = _marker_estado(sid) is not None
            break
        if _cedido(sid):
            pwlog("cot-vivo %s: otro vigilante tomó la sesión — me retiro"
                  % sid[-8:])
            return CEDIDO, ultimo
        pid_latir(sid)
        if time.time() >= proximo_aviso:
            proximo_aviso = time.time() + max(config.TURNO_LARGO_AVISO_S, 60)
            uhttp.send(cfg, "⏱️ «%s» lleva %d min en la Mac — sigo con él "
                       "(el Stop de la app lo frena)."
                       % (esc(titulo), int((time.time() - t0) / 60)),
                       loud=False)
        est = _marker_estado(sid)
        if est is None or _otro_turno(ini, est):
            corte = est
            break
        if not ini[1] and est[1]:
            ini = est  # the token arrived whole: from here on it is compared by token
        msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
        if after_id and not msgs:
            vacios += 1
            if vacios == COT_VIVO_VACIO_POLLS:
                # warm pickup without even the new prompt in sight: the
                # connection is not reflecting the store — a resume (the
                # usual cost, only in this anomalous case) hooks it back up
                pwlog("cot-vivo %s: %d polls sin historial nuevo — re-resume"
                      % (sid[-8:], vacios))
                c.request("session/resume", {"sessionId": sid},
                          timeout=COT_RESUME_TIMEOUT_S)
        else:
            vacios = 0
        cajas, rotulos, pend, k = cajas_vivas(msgs)
        if len(cajas) != visto:
            vivas = (cajas, rotulos)
            # F46: one button box PER PASS — the first is born right away
            # (anti-silence while resuming), the others with their first
            # box sealed; the previous one stays closed with its own final
            # status.
            for i in range(max(visto, 0), len(cajas)):
                r = _raz_de(rotulos[i]) if i < len(rotulos) else 0
                if r and r != r_actual:
                    if ph and r_actual:
                        phs_map[r_actual] = ph
                        _edit_fin_razonada(cfg, ph, sid, t0, titulo,
                                           r_actual, cajas, rotulos,
                                           ph_ini, i + 1)
                        ph = None
                    # F49: the anti-silence message (r_actual=0, born with
                    # 0 boxes) is ADOPTED as the first pass's — before, it
                    # closed as «pass 0 — 0 boxes» and the pass was born in
                    # another message: an empty stub above every turn that
                    # started mid-thought
                    r_actual, ph_ini = r, i + 1
            texto = texto_vivo(titulo, len(cajas) - ph_ini + 1, k, pend)
            kb = {"inline_keyboard": botones_cajas(
                sid, t0, rotulos[ph_ini - 1:], ini=ph_ini)}
            _persistir(vivo=True)
            if ph:
                if uhttp._live_edit(cfg, ph, texto, markup=kb):
                    log_teclado("mac viva r%d" % r_actual, ph, kb)
            else:
                ph = ph_de(uhttp.send(cfg, texto, loud=False, markup=kb))
                if ph:
                    phs_map[r_actual] = ph
                    _persistir(vivo=True)
                    log_teclado("mac nace r%d" % r_actual, ph, kb)
            visto = len(cajas)
        time.sleep(COT_VIVO_CAD_S)
    msgs = []
    if corte is None:
        # closed cleanly (or ceiling): the history already has the whole turn
        msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
        cajas, rotulos = cajas_del_turno(msgs)
        ultimo = _last_id_of(msgs) or ultimo
    else:
        # on_stop → new prompt race: the history already carries the NEXT
        # turn (its user is the last one) — what was shown live is what
        # there is
        cajas, rotulos = vivas
    if len(cajas) < len(vivas[0]):
        cajas, rotulos = vivas  # monotonic guard: never less than what was shown
    if ph:
        phs_map[r_actual] = ph
    if not cajas and msgs and not techo and cfg.get("cot_aviso_vacio"):
        # F45: closed cleanly, history in plain sight and not one
        # reasoning part → say so: in the live box if it was born (before
        # it stayed at «0 boxes, 0 chars. Tap one to read it» with no
        # buttons), otherwise in one line. With a ceiling or a foreign cut
        # nothing is known: stay silent.
        if ph:
            uhttp._live_edit(cfg, ph, texto_vacio(titulo, t0))
        else:
            uhttp.send(cfg, texto_vacio(titulo, t0), loud=False)
    elif ph:
        # F49: the pass on screen closes like all the others — ITS
        # message, ITS buttons. Before, the last message turned into an
        # index with ALL the turn's boxes (Damian: «they all piled up in
        # the last box, it is running really badly»).
        idx = [i + 1 for i, rt in enumerate(rotulos)
               if _raz_de(rt) == r_actual]
        if idx:
            _edit_fin_razonada(cfg, ph, sid, t0, titulo, r_actual, cajas,
                               rotulos, idx[0], idx[-1] + 1)
        else:
            # the message on screen has no boxes of its own: it was born
            # anti-silence and the turn did not reason, another prompt cut
            # it, or the boxes only arrived in this fetch. A single pass →
            # the message IS its own; several (or none) → the usual close,
            # and if there are boxes they all live in THAT message
            rs = sorted({_raz_de(rt) for rt in rotulos})
            if len(rs) == 1 and rs[0]:
                phs_map[rs[0]] = ph
                _edit_fin_razonada(cfg, ph, sid, t0, titulo, rs[0], cajas,
                                   rotulos, 1, len(rotulos) + 1)
            else:
                kb = {"inline_keyboard": botones_cajas(sid, t0, rotulos)}
                if uhttp._live_edit(cfg, ph, texto_final(
                        titulo, t0, len(cajas), sum(len(b) for b in cajas)),
                        markup=kb):
                    for rt in rotulos:
                        phs_map[_raz_de(rt)] = ph
                    if rotulos:
                        log_teclado("mac unico", ph, kb)
    elif cajas:
        # nothing was born live (the boxes only arrived in this fetch):
        # the usual close, ONE message with all of them
        kb = {"inline_keyboard": botones_cajas(sid, t0, rotulos)}
        ph = ph_de(uhttp.send(cfg, texto_final(
            titulo, t0, len(cajas), sum(len(b) for b in cajas)),
            loud=False, markup=kb))
        if ph:
            for rt in rotulos:
                phs_map[_raz_de(rt)] = ph
            log_teclado("mac unico", ph, kb)
    # F49: passes without their own message (born in the close-time
    # fetch): ONE late-boxes message with THOSE boxes — no button is lost
    # and nothing piles up in another pass's message
    sin = [i + 1 for i, p in enumerate(_phs_de(rotulos, phs_map)) if not p]
    if sin:
        kb = {"inline_keyboard": botones_indices(sid, t0, rotulos, sin)}
        phz = ph_de(uhttp.send(cfg, texto_rezagadas(titulo, len(sin), t0),
                               loud=False, markup=kb))
        if phz:
            for i in sin:
                phs_map[_raz_de(rotulos[i - 1])] = phz
            log_teclado("mac tardias", phz, kb)
    _persistir(finalizado=True)
    pwlog("cot-vivo fin: cajas=%d" % len(cajas))
    return (TECHO if techo else corte), ultimo


def _soltar(cfg, sid):
    """Reasons for the warm vigilante to release the runtime: /stop, the
    task stopped being current, or a bot worker took the session (phone:
    its LiveTurnView already makes the boxes — here they would be
    duplicated)."""
    return _stop_requested(sid) or not _task_current_ok(cfg, sid) \
        or _worker_owned(sid)


def _cot_warm_wait(cfg, sid, timeout_s):
    """F44 — the WARM wait between turns: no runtime, reading small files
    every 1s, until the turn's marker reappears (a new Mac prompt) → its
    (mtime, token). None = release the runtime: keepalive expired,
    _soltar(), or a successor took the pid-file."""
    t_fin = time.time() + timeout_s
    while time.time() < t_fin:
        if _cedido(sid):
            pwlog("cot-vivo %s: otro vigilante tomó la sesión — me retiro"
                  % sid[-8:])
            return None
        if _soltar(cfg, sid):
            return None
        est = _marker_estado(sid)
        if est is not None:
            return est
        if config.codigo_cambio():
            # F48d: there was a deploy — the hook spawns a new vigilante
            # (with the new code) on the Mac's next turn
            pwlog("cot-vivo %s: código nuevo deployado — me retiro"
                  % sid[-8:])
            return None
        pid_latir(sid)
        time.sleep(COT_VIVO_WAIT_S)
    # last look: the hook that saw us alive an instant ago did NOT spawn
    # another one — a marker that landed right at expiry is just as much
    # ours
    est = _marker_estado(sid)
    if est is not None and not _cedido(sid) and not _soltar(cfg, sid):
        return est
    pwlog("cot-vivo %s: keepalive vencido (%ds) — runtime liberado"
          % (sid[-8:], timeout_s))
    return None


def vivir(cfg, sid, titulo, t0, ws, keepalive=None):
    """F41/F44 — the Mac CoT vigilante: ONE resume per SESSION. It watches
    the turn that launched it and, with keepalive, stays WARM waiting for
    the same session's next marker — same connection, no second resume:
    the following turns' first little box arrives in seconds. Each turn's
    cache is written as it is taken: on_stop skips F40's --cot-botones
    (the vigilante is the owner). keepalive 0 = one turn and done (F41)."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if keepalive is None:
        keepalive = int(cfg.get("cot_vivo_keepalive_s") or 0)
    ini = _marker_estado(sid) or (int(t0), "")  # the turn I was sent to watch
    guardar(sid, t0, titulo, [], [], vivo=True)  # owner: F40 does not duplicate
    pid_escribir(sid)
    c = None
    try:
        c = zc_proto.make(cwd=ws)
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
        if e := zc_proto.proto_err(r, "resume"):
            pwlog("cot-vivo %s: %s" % (sid[-8:], e[:120]))
            guardar(sid, t0, titulo, [], [], finalizado=True, error=e[:120])
            return
        ultimo, turnos = None, 0
        while True:
            corte, ultimo = _vigilar_turno(c, cfg, sid, titulo, t0, ini, ultimo)
            turnos += 1
            if corte is CEDIDO or corte is TECHO:
                break
            if corte is None:
                if keepalive <= 0:
                    break
                corte = _cot_warm_wait(cfg, sid, keepalive)
                if corte is None:
                    break
            # F44: new turn of the SAME session, same connection. The title
            # is recalculated the way the hook would on spawn (the tasks
            # index, or the workspace folder)
            ini, t0 = corte, corte[0]
            titulo = task_label({"session_id": sid, "cwd": ws}) or titulo
            guardar(sid, t0, titulo, [], [], vivo=True)  # possession: F40 does not duplicate
        if turnos > 1:
            pwlog("cot-vivo %s: %d turnos con un solo resume" % (sid[-8:], turnos))
    finally:
        pid_borrar(sid)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def cmd_cot_vivo(sid, titulo, t0, ws):
    """Entrypoint of the detached subprocess (gateway --cot-vivo)."""
    cfg = load_config()
    try:
        vivir(cfg, sid, titulo, float(t0), ws)
    except Exception as exc:  # noqa: BLE001 - the subprocess reports, it does not die
        pwlog("cot-vivo %s falló: %s" % (sid[-8:], exc))


def cmd_cot_botones(sid, titulo, t0, ws):
    """Entrypoint of the detached subprocess (gateway --cot-botones)."""
    cfg = load_config()
    try:
        k = capturar(cfg, sid, titulo, float(t0), ws)
        pwlog("cot-botones %s: %s cajas" % (sid[-8:], k))
    except Exception as exc:  # noqa: BLE001 - the subprocess reports, it does not die
        pwlog("cot-botones %s falló: %s" % (sid[-8:], exc))
