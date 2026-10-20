"""razonamiento — el CoT en CAJAS por tamaño, con botones (F40-F44).

Modelo de Damian (F43, textual): "una caja tiene un tamaño; cuando el CoT
llega al tope se llena la caja y se envía con su número; continúa el CoT
y de nuevo frena en el tope, y así incrementalmente van llegando botones
hasta que el CoT termine; luego el chat escribe o manda formulario; y
cuando vuelva a pensar, manda CoT de nuevo numerado desde 1". O sea:
CAJAS SELLADAS POR TAMAÑO durante la razonada (no por fase), botón por
caja en cuanto se llena, y numeración que REINICIA en cada razonada.

Fuentes (las tres escriben el mismo cache y usan los mismos botones):
- F42 celu: LiveTurnView alimenta un SelladorCot con los reasoning_delta.
- F41 Mac: `--cot-vivo` sondea el historial (el part reasoning del paso
  en curso ya es legible a mitad de paso — verificado en vivo 20/10).
  F44: el vigilante queda CALIENTE entre turnos de la misma sesión — UN
  resume por sesión, no por turno (el resume de bot_development tardaba
  ~45s y la primera cajita llegaba a los ~48s de CADA prompt). El pid-file
  cot-vivo-<sid>.pid (pid + heartbeat en el mtime) es el single-flight
  del hook: con un vigilante vivo y fresco no se spawnea otro.
- F40 Mac (fallback al cierre): `--cot-botones` captura post-turno.

El tap (ct:sid:t0:n[:p] → /cotb) lee el cache: sin runtime, instantáneo.
COT_CAJA_CAP = COT_PARTE_CAP → cada caja abre ENTERA en un mensaje, sin
sub-botones de parte. El cache guarda `ph` (message_id de la caja de
botones) para que el tap edite EN el mensaje (F43c): send devuelve ese id
y cada reescritura del cache lo conserva.

F45: un turno que cierra SIN razonamiento lo dice en una línea (texto_vacio)
en vez de callarse — las tres fuentes. Diagnóstico 20/10 sobre el rollout
crudo del runtime: el bot pide thinking IDÉNTICO para GLM-5.3 y Flash
(thinking enabled + effort max); GLM-5.3 razona de forma ADAPTATIVA — decide
por llamada — y en la sesión del bot pasó 12 llamadas seguidas sin razonar
con ~130k tokens de contexto (volvió al compactarse); Flash razona casi
siempre. Sin el aviso, cero cajitas era indistinguible de un bug.
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

COT_PARTE_CAP = 3000      # chars por parte al ABRIR una caja vieja/grande
COT_CAJA_CAP = COT_PARTE_CAP  # chars por CAJA: al tope se sella y viaja su botón
COT_BOTONES_MAX = 20      # teclado: más cajas → nota de /cot (documento)
COT_BOTONES_FILA = 5
COT_MSGS = 60             # mensajes a traer: el turno de la Mac más largo entra
COT_RESUME_TIMEOUT_S = 120
ARCH_COT = "cot-%s-%d.json"
ARCH_COT_PID = "cot-vivo-%s.pid"  # F44: pid del vigilante caliente de la sesión


def cache_path(sid, t0):
    return os.path.join(config.STATE_DIR, ARCH_COT % (sid, int(t0)))


def ph_de(res):
    """message_id utilizable de lo que devolvió uhttp.send: True (salió
    pero sin id) no sirve para editar — y bool ES subclase de int."""
    return res if isinstance(res, int) and not isinstance(res, bool) else None


def _con_ph(ph):
    return {"ph": ph} if ph else {}


# ------------------------------------------------------------- el sellador

class SelladorCot:
    """Sella cajas por TAMAÑO dentro de UNA razonada: alimentar() devuelve
    las cajas que se llenaron (con su número local, desde 1); cerrar()
    sella la cola como última caja de la razonada y REINICIA el estado
    para la próxima. El rótulo VISIBLE lo arma el llamador: «CoT R.C»
    (F46) — razonada R, caja C dentro de ella — así nunca hay dos
    «CoT 1». Sin estados raros: el texto acumulado se conserva entero
    hasta que la razonada cierra."""

    def __init__(self, cap=COT_CAJA_CAP):
        self.cap = cap
        self.texto = ""
        self.selladas = 0

    def alimentar(self, delta):
        """[(caja, n_local)] de las cajas que alcanzaron el tope."""
        self.texto += delta
        nuevas = []
        while len(self.texto) >= self.cap * (self.selladas + 1):
            self.selladas += 1
            corte = self.cap * self.selladas
            nuevas.append((self.texto[corte - self.cap:corte],
                           self.selladas))
        return nuevas

    def cerrar(self):
        """Fin de la razonada: la cola (si trae algo) es la última caja.
        El estado reinicia para la próxima razonada."""
        cola = self.texto[self.cap * self.selladas:]
        n = self.selladas + 1
        self.texto, self.selladas = "", 0
        return [(cola, n)] if cola.strip() else []

    @property
    def en_curso(self):
        """(chars de la caja que se está llenando, n_local de esa caja)."""
        resto = len(self.texto) - self.cap * self.selladas
        return max(0, resto), self.selladas + 1


def cajas_de_texto(texto, cap=COT_CAJA_CAP):
    """Cajas de una razonada YA terminada: llenas al tope + cola final.
    [(caja, n_local)] con la numeración desde 1."""
    return [(texto[i:i + cap], i // cap + 1)
            for i in range(0, len(texto), cap)] if texto.strip() else []


# ------------------------------------------- del historial (Mac: F40/F41)

def _partes_del_turno(msgs):
    """Parts reasoning del turno (posterior al último user) AGRUPADAS en
    razonadas: una razonada termina cuando el modelo HACE algo (part
    tool/text entre reasonings); reasonings contiguos son la misma.
    [(razonada_texto, en_curso_bool)] — la última puede seguir creciendo
    (es la última part del último mensaje)."""
    ini = 0
    for i, m in enumerate(msgs):
        rol = (m.get("info") or {}).get("role") or m.get("role")
        if rol == "user":
            ini = i + 1
    razonadas, actual, en_curso = [], [], False
    ultima = None
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
                # el modelo hizo algo entre reasonings: la razonada cerró
                razonadas.append("".join(actual))
                actual, en_curso = [], False
    if actual:
        razonadas.append("".join(actual))
    return razonadas, en_curso


def _cajas_de_razonadas(razonadas, hasta=None, base_r=0):
    """(cajas, rotulos) de las razonadas dadas — el loop que
    cajas_del_turno y cajas_vivas gemelaban. hasta = solo las primeras N
    (la de en curso queda afuera); base_r = numeración inicial (la cola
    de cajas_vivas sigue la numeración de la ÚLTIMA razonada)."""
    cajas, rotulos = [], []
    trozadas = razonadas[:hasta] if hasta is not None else razonadas
    for r, texto in enumerate(trozadas, base_r + 1):
        for caja, n in cajas_de_texto(texto):
            cajas.append(caja)
            rotulos.append("CoT %d.%d" % (r, n))
    return cajas, rotulos


def cajas_del_turno(msgs):
    """Cajas COMPLETAS del turno (turno ya cerrado): cada razonada llena
    sus cajas al tope + cola. F46: rótulo «CoT R.C» (razonada R, caja C)
    — único, nunca dos «CoT 1». → (cajas, rotulos) paralelos."""
    razonadas, _en_curso = _partes_del_turno(msgs)
    return _cajas_de_razonadas(razonadas)


def cajas_vivas(msgs):
    """Ídem cajas_del_turno pero para el turno EN MARCHA: la razonada en
    curso solo aporta las cajas YA LLENAS (la que se llena ahora, no).
    → (cajas, rotulos, chars_en_curso, rotulo_en_curso «R.C»)."""
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


# ------------------------------------------------------------- botones

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
    """Un botón por caja; rótulos «CoT R.C» (F46: nunca dos «CoT 1») y el
    callback_data lleva el índice GLOBAL: ct:sid:t0:n. ini = índice
    global del primer rótulo (F46: el teclado de UNA razonada es un
    slice del total)."""
    return _filas([{"text": rotulos[i - ini][:36],
                    "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                   for i in range(ini,
                                  ini + min(len(rotulos), COT_BOTONES_MAX))])


def botones_partes(sid, t0, n, total, actual):
    return _filas([{"text": ("[%d/%d]" if p == actual else "%d/%d")
                    % (p, total),
                    "callback_data": "ct:%s:%d:%d:%d" % (sid, int(t0), n, p)}
                   for p in range(1, total + 1)])


# ------------------------------------------------------------- cache

def guardar(sid, t0, titulo, cajas, rotulos, **extra):
    """Escribe/actualiza el cache de un turno (las tres fuentes). extra:
    vivo/finalizado. 'bloques' se mantiene por compatibilidad con caches
    viejos (mostrar lo acepta)."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(cache_path(sid, t0), "w") as f:
        json.dump({"sid": sid, "titulo": titulo, "t0": int(t0),
                   "cajas": cajas, "rotulos": rotulos, **extra}, f)


def cache_existe(sid, t0):
    return os.path.exists(cache_path(sid, t0))


def cerrar_cache(sid, t0, **extra):
    """F47d — cierra el cache de un turno que murió SIN su cierre (worker
    rematado por /stop, kill -9, OOM): el último guardar del LiveTurnView
    fue vivo=True y así quedaba para siempre. Conserva cajas/rótulos/ph/phs
    — solo quita vivo, marca finalizado y suma extra (p.ej. error=). False
    si no hay cache (turno sin cajas) o no se pudo leer/escribir."""
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
        with open(cache_path(sid, t0), "w") as f:
            json.dump(d, f)
    except OSError:
        return False
    return True


def texto_vivo(titulo, n, en_curso="", pend=0):
    """Estado de la caja de botones: cajas selladas + la que se llena
    (en_curso = rótulo «R.C» de la caja llenándose)."""
    linea = "🧠 <b>«%s»</b> pensando… · %d caja%s" % (
        esc(titulo or "?"), n, "s" if n != 1 else "")
    if pend:
        linea += " · llenando la CoT %s (%d chars)" % (en_curso, pend)
    if n:
        linea += "\nTocá un botón para leerlo:"
    return linea


def texto_razonada(titulo, r, n, total):
    """F46 — cierre de UNA razonada: su caja de botones queda con estado
    final propio (el turno sigue: la próxima razonada nace en un mensaje
    nuevo, en su posición cronológica)."""
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


def texto_vacio(titulo, t0):
    """F45 — el turno cerró y no hubo NI UN part/delta de razonamiento: una
    línea que lo diga. Sin esto el chat quedaba mudo y cero cajitas era
    indistinguible de un bug (Damian, 20/10: 'arreglalo para que haya cot
    en los dos modelos' — el bot ya pedía thinking igual para ambos)."""
    hora = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    return ("🧠 <b>«%s»</b> (turno de las %s) — <b>sin CoT</b>: el modelo no "
            "razonó en este turno (thinking adaptativo: decide por llamada; "
            "GLM-5.3 suele saltearlo con el contexto muy largo — compactar "
            "lo trae de vuelta; Flash razona casi siempre)."
            % (esc(titulo or "?"), hora))


# ------------------------------------------------------------- fuentes

def capturar(cfg, sid, titulo, t0, ws):
    """F40 — fallback al cerrar un turno de la Mac (si el vivo no pudo):
    trae el historial, cachea las cajas y manda el mensaje con botones.
    Devuelve la cantidad de cajas: 0 si el turno cerró sin razonar (F45: se
    avisa en una línea), None si falló el resume o no hubo historial."""
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
            uhttp.send(cfg, texto_vacio(titulo or sid[-8:], t0), loud=False)
        return 0
    guardar(sid, t0, titulo, cajas, rotulos)
    total = sum(len(b) for b in cajas)
    ph = ph_de(uhttp.send(cfg, texto_final(titulo or sid[-8:], t0, len(cajas),
                                            total), loud=False,
                          markup={"inline_keyboard": botones_cajas(sid, t0,
                                                                   rotulos)}))
    if ph:
        guardar(sid, t0, titulo, cajas, rotulos, ph=ph)  # F43c
    return len(cajas)


def mostrar(sid, t0, n, parte=1):
    """El tap: caja n del cache (rótulo local; índice global en el botón)."""
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
    partes = partes_iguales(cajas[n - 1], COT_PARTE_CAP)
    parte = min(max(parte, 1), len(partes))
    cab = "🧠 <b>%s</b> · «%s»" % (esc(rotulo),
                                   esc(d.get("titulo") or sid[-8:]))
    if len(partes) == 1:
        return cab + "\n<pre>%s</pre>" % esc(partes[0])
    cab += " · parte %d/%d" % (parte, len(partes))
    return (cab + "\n<pre>%s</pre>" % esc(partes[parte - 1]),
            {"inline_keyboard": botones_partes(sid, t0, n, len(partes),
                                               parte)})


# ------------------------------------------------------------- F41: Mac en vivo

COT_VIVO_CAD_S = 3.0      # cadencia del polling al historial
COT_VIVO_MAX_S = 35 * 60  # techo del vigilante (el mismo del espectador)
COT_VIVO_WAIT_S = 1.0     # F44: cadencia de la espera caliente (archivos chicos, sin runtime)
COT_VIVO_HB_FRESCO_S = 90  # F44: heartbeat más viejo que esto = vigilante colgado (> los 60s
#                            de timeout de session/messages: un fetch lento no es un colgado)
COT_VIVO_VACIO_POLLS = 5  # F44: polls seguidos sin NADA tras after_id (~15s) → re-resume
#                            (el prompt nuevo tiene que estar: si no se ve, la conexión
#                            no está viendo el store — un resume la refresca)
CEDIDO = object()         # F44: un sucesor tomó el pid-file — este vigilante se retira
TECHO = object()          # F44: 35 min y el marker sigue — no se re-vigila el mismo turno


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", sid or "ses")


def pid_path(sid):
    return os.path.join(config.STATE_DIR, ARCH_COT_PID % _safe_sid(sid))


def pid_escribir(sid):
    """El vigilante se registra: su pid en el archivo, el heartbeat en el
    mtime. El hook lo lee para no spawnear un segundo vigilante."""
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
    """Solo si el archivo sigue siendo MÍO: un sucesor pudo tomarlo."""
    if pid_leer(sid) == os.getpid():
        try:
            os.remove(pid_path(sid))
        except OSError:
            pass


def _cedido(sid):
    """Otro pid en el pid-file = un sucesor tomó la sesión (el hook vio
    el heartbeat viejo). Sin archivo no hay sucesor."""
    return pid_leer(sid) not in (0, os.getpid())


def vigilante_activo(sid, proc_vivo=None, now=None, fresco_s=COT_VIVO_HB_FRESCO_S):
    """F44 single-flight del hook: ¿hay un vigilante caliente de esta
    sesión? pid-file con pid vivo y heartbeat fresco. Muerto o colgado →
    False: el hook spawnea otro, que toma el pid-file."""
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
        from zcode_bot.workers import _proc_vivo  # lazy: workers → live_view → acá
        proc_vivo = _proc_vivo
    return bool(proc_vivo(pid))


def vigilantes_pids():
    """pids de los pid-files cot-vivo-*.pid: /nuclear los remata con los
    workers (verificando el cmdline: un archivo huérfano puede apuntar a
    un pid reciclado)."""
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
    """(mtime_int, token) del marker del turno en vuelo; None si no hay
    turno. token "" = marker a mitad de escritura (el hook trunca y
    escribe): se resuelve en el próximo poll, no es un turno nuevo."""
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
    """¿El marker que veo es de OTRO turno que el que arranqué a vigilar?
    Por token (único por prompt) cuando los dos se leyeron; por mtime si no."""
    (mt0, tok0), (mt, tok) = ini, est
    if tok0 and tok:
        return tok != tok0
    return mt != mt0


def _raz_de(rotulo):
    """«CoT R.C» → R (0 si no parsea: rótulos viejos «CoT N» sin punto)."""
    try:
        rc = str(rotulo).split()[1]
        return int(rc.split(".")[0]) if "." in rc else 0
    except (IndexError, ValueError):
        return 0


def _phs_de(rotulos, phs_map):
    """F46 — lista de message_id por caja (alineada a rotulos): el tap
    edita el mensaje de SU razonada; None donde no hubo mensaje."""
    return [phs_map.get(_raz_de(rt)) for rt in rotulos]


def cierre_razonada(sid, t0, titulo, r, cajas, rotulos, ini, fin):
    """R9 — (texto, teclado) del cierre de UNA razonada: estado final
    propio con SUS botones (slice global). La fórmula que el celu
    (live_view._cot_run_cerrar) y el vigilante de la Mac
    (_edit_fin_razonada) duplicaban — pura, cada uno la publica con su
    propio transporte. ini/fin = índices globales 1-based de sus cajas."""
    return (texto_razonada(titulo, r, fin - ini,
                           sum(len(b) for b in cajas[ini - 1:fin - 1])),
            {"inline_keyboard": botones_cajas(sid, t0, rotulos[ini - 1:fin - 1],
                                              ini=ini)})


def _edit_fin_razonada(cfg, ph, sid, t0, titulo, r, cajas, rotulos, ini, fin):
    """F46 — cierre de la caja de botones de UNA razonada (el vigilante
    de la Mac): estado final propio con SUS botones; la próxima razonada
    nace en un mensaje nuevo (posición cronológica: cajitas de la fase 1,
    tools/diffs, cajitas de la fase 2…)."""
    texto, kb = cierre_razonada(sid, int(t0), titulo, r, cajas, rotulos,
                                ini, fin)
    try:
        uhttp._live_edit(cfg, ph, texto, markup=kb)
    except Exception:  # noqa: BLE001 — el cierre jamás rompe el vigilante
        pass


def _vigilar_turno(c, cfg, sid, titulo, t0, ini, after_id=None):
    """UN turno de la Mac: sondea el historial cada 3s hasta que el marker
    desaparece (on_stop: cerró), lo reemplaza otro (prompt nuevo antes de
    que lo viéramos cerrar) o techo de 35 min. Cada caja que se llena
    agrega su botón; la caja de botones NACE con la primera.

    after_id: último mensaje del turno ANTERIOR — lo de este turno es lo
    posterior. En el pickup caliente (F44) el prompt nuevo puede no estar
    persistido en el primer poll: sin el corte, el último user del
    historial sería el del turno anterior y sus cajas viajarían otra vez.

    → (corte, ultimo): corte None = cerró limpio; (mtime, token) del
    marker ajeno que lo cortó; CEDIDO si otro vigilante tomó el pid-file;
    TECHO si pasaron 35 min con el marker todavía ahí (se cierra con lo
    que hay y NO se re-vigila: un marker huérfano sería un loop eterno).
    ultimo = id del último mensaje visto (after_id del próximo turno)."""
    visto, ph = -1, None
    phs_map = {}             # F46: razonada → message_id de SU caja de botones
    r_actual, ph_ini = 0, 1  # razonada en pantalla y su primer índice global
    vivas = ([], [])         # lo último mostrado en vivo (guardia monótona)

    def _persistir(**extra):
        """R9 — el guardar del vigilante SIEMPRE con los phs al día (el
        cuarteto guardar+phs+_phs_de+_con_ph que se repetía)."""
        guardar(sid, t0, titulo, cajas, rotulos,
                phs=_phs_de(rotulos, phs_map), **_con_ph(ph), **extra)
    ultimo, corte, techo, vacios = after_id, None, False, 0
    t_fin = time.time() + COT_VIVO_MAX_S
    while True:
        if time.time() >= t_fin:
            techo = _marker_estado(sid) is not None
            break
        if _cedido(sid):
            pwlog("cot-vivo %s: otro vigilante tomó la sesión — me retiro"
                  % sid[-8:])
            return CEDIDO, ultimo
        pid_latir(sid)
        est = _marker_estado(sid)
        if est is None or _otro_turno(ini, est):
            corte = est
            break
        if not ini[1] and est[1]:
            ini = est  # el token llegó entero: de acá en más se compara por él
        msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
        if after_id and not msgs:
            vacios += 1
            if vacios == COT_VIVO_VACIO_POLLS:
                # pickup caliente sin ver ni el prompt nuevo: la conexión no
                # está reflejando el store — un resume (el costo de siempre,
                # solo en este caso anómalo) la vuelve a enganchar
                pwlog("cot-vivo %s: %d polls sin historial nuevo — re-resume"
                      % (sid[-8:], vacios))
                c.request("session/resume", {"sessionId": sid},
                          timeout=COT_RESUME_TIMEOUT_S)
        else:
            vacios = 0
        cajas, rotulos, pend, k = cajas_vivas(msgs)
        if len(cajas) != visto:
            vivas = (cajas, rotulos)
            # F46: una caja de botones POR RAZONADA — la primera nace ya
            # (anti-silencio del resume), las demás con su primera caja
            # sellada; la anterior queda cerrada con estado final propio.
            for i in range(max(visto, 0), len(cajas)):
                r = _raz_de(rotulos[i]) if i < len(rotulos) else 0
                if r and r != r_actual:
                    if ph:
                        phs_map[r_actual] = ph
                        _edit_fin_razonada(cfg, ph, sid, t0, titulo,
                                           r_actual, cajas, rotulos,
                                           ph_ini, i + 1)
                    r_actual, ph_ini, ph = r, i + 1, None
            texto = texto_vivo(titulo, len(cajas) - ph_ini + 1, k, pend)
            kb = {"inline_keyboard": botones_cajas(
                sid, t0, rotulos[ph_ini - 1:], ini=ph_ini)}
            _persistir(vivo=True)
            if ph:
                uhttp._live_edit(cfg, ph, texto, markup=kb)
            else:
                ph = ph_de(uhttp.send(cfg, texto, loud=False, markup=kb))
                if ph:
                    phs_map[r_actual] = ph
                    _persistir(vivo=True)
            visto = len(cajas)
        time.sleep(COT_VIVO_CAD_S)
    msgs = []
    if corte is None:
        # cerró limpio (o techo): el historial ya tiene el turno entero
        msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
        cajas, rotulos = cajas_del_turno(msgs)
        ultimo = _last_id_of(msgs) or ultimo
    else:
        # carrera on_stop → prompt nuevo: el historial ya trae el turno
        # SIGUIENTE (su user es el último) — lo visto en vivo es lo que hay
        cajas, rotulos = vivas
    if len(cajas) < len(vivas[0]):
        cajas, rotulos = vivas  # guardia monótona: nunca menos que lo mostrado
    if ph:
        phs_map[r_actual] = ph
    # F46: razonadas nacidas en el fetch de cierre no tienen mensaje propio
    # — el índice final (el mensaje de la última razonada) las cubre.
    _persistir(finalizado=True)
    total = sum(len(b) for b in cajas)
    kb = {"inline_keyboard": botones_cajas(sid, t0, rotulos)}
    if not cajas and msgs and not techo and cfg.get("cot_aviso_vacio"):
        # F45: cerró limpio, con el historial a la vista y ni un part
        # reasoning → decirlo: en la caja viva si nació (antes quedaba en
        # «0 cajas, 0 chars. Tocá una para leerla» sin botones), si no en
        # una línea. Con techo o corte ajeno no se sabe: callar.
        if ph:
            uhttp._live_edit(cfg, ph, texto_vacio(titulo, t0))
        else:
            uhttp.send(cfg, texto_vacio(titulo, t0), loud=False)
    elif ph:
        uhttp._live_edit(cfg, ph, texto_final(titulo, t0, len(cajas), total),
                         markup=kb)
    elif cajas:
        ph = ph_de(uhttp.send(cfg, texto_final(titulo, t0, len(cajas), total),
                              loud=False, markup=kb))
        if ph:
            _persistir(finalizado=True)
    pwlog("cot-vivo fin: cajas=%d" % len(cajas))
    return (TECHO if techo else corte), ultimo


def _soltar(cfg, sid):
    """Motivos para que el vigilante caliente suelte el runtime: /stop, la
    task dejó de ser current, o un worker del bot tomó la sesión (el celu:
    su LiveTurnView ya hace las cajas — acá serían dobles)."""
    return _stop_requested(sid) or not _task_current_ok(cfg, sid) \
        or _worker_owned(sid)


def _cot_warm_wait(cfg, sid, timeout_s):
    """F44 — la espera CALIENTE entre turnos: sin runtime, leyendo archivos
    chicos cada 1s, hasta que el marker del turno reaparezca (prompt nuevo
    de la Mac) → su (mtime, token). None = soltar el runtime: venció el
    keepalive, _soltar(), o un sucesor tomó el pid-file."""
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
        pid_latir(sid)
        time.sleep(COT_VIVO_WAIT_S)
    # última mirada: el hook que nos vio vivos hace un instante NO spawneó
    # otro — un marker que entró justo al vencer es nuestro igual
    est = _marker_estado(sid)
    if est is not None and not _cedido(sid) and not _soltar(cfg, sid):
        return est
    pwlog("cot-vivo %s: keepalive vencido (%ds) — runtime liberado"
          % (sid[-8:], timeout_s))
    return None


def vivir(cfg, sid, titulo, t0, ws, keepalive=None):
    """F41/F44 — el vigilante del CoT de la Mac: UN resume por SESIÓN.
    Vigila el turno que lo lanzó y, con keepalive, queda CALIENTE esperando
    el próximo marker de la misma sesión — misma conexión, sin segundo
    resume: la primera cajita de los turnos siguientes llega en segundos.
    El cache de cada turno se escribe al tomarlo: on_stop salta el
    --cot-botones de F40 (el vigilante es el dueño). keepalive 0 = un
    turno y chau (F41)."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if keepalive is None:
        keepalive = int(cfg.get("cot_vivo_keepalive_s") or 0)
    ini = _marker_estado(sid) or (int(t0), "")  # el turno que me mandaron a vigilar
    guardar(sid, t0, titulo, [], [], vivo=True)  # dueño: F40 no duplica
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
            # F44: turno nuevo de la MISMA sesión, misma conexión. El título
            # se recalcula como lo haría el hook al spawnear (índice de
            # tasks, o la carpeta del workspace)
            ini, t0 = corte, corte[0]
            titulo = task_label({"session_id": sid, "cwd": ws}) or titulo
            guardar(sid, t0, titulo, [], [], vivo=True)  # posesión: F40 no duplica
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
    """Entrypoint del subproceso detached (gateway --cot-vivo)."""
    cfg = load_config()
    try:
        vivir(cfg, sid, titulo, float(t0), ws)
    except Exception as exc:  # noqa: BLE001 - el subproceso reporta, no muere
        pwlog("cot-vivo %s falló: %s" % (sid[-8:], exc))


def cmd_cot_botones(sid, titulo, t0, ws):
    """Entrypoint del subproceso detached (gateway --cot-botones)."""
    cfg = load_config()
    try:
        k = capturar(cfg, sid, titulo, float(t0), ws)
        pwlog("cot-botones %s: %s cajas" % (sid[-8:], k))
    except Exception as exc:  # noqa: BLE001 - el subproceso reporta, no muere
        pwlog("cot-botones %s falló: %s" % (sid[-8:], exc))
