"""razonamiento — el CoT en CAJAS por tamaño, con botones (F40-F43).

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
- F40 Mac (fallback al cierre): `--cot-botones` captura post-turno.

El tap (ct:sid:t0:n[:p] → /cotb) lee el cache: sin runtime, instantáneo.
COT_CAJA_CAP = COT_PARTE_CAP → cada caja abre ENTERA en un mensaje, sin
sub-botones de parte.
"""
import datetime
import json
import os
import time

from zcode_bot import config
from zcode_bot.config import marker_path
from zcode_bot import proto as zc_proto
from zcode_bot.messages import _fetch_messages
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, partes_iguales
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import pwlog

COT_PARTE_CAP = 3000      # chars por parte al ABRIR una caja vieja/grande
COT_CAJA_CAP = COT_PARTE_CAP  # chars por CAJA: al tope se sella y viaja su botón
COT_BOTONES_MAX = 20      # teclado: más cajas → nota de /cot (documento)
COT_BOTONES_FILA = 5
COT_MSGS = 60             # mensajes a traer: el turno de la Mac más largo entra
COT_RESUME_TIMEOUT_S = 120
ARCH_COT = "cot-%s-%d.json"


def cache_path(sid, t0):
    return os.path.join(config.STATE_DIR, ARCH_COT % (sid, int(t0)))


# ------------------------------------------------------------- el sellador

class SelladorCot:
    """Sella cajas por TAMAÑO dentro de UNA razonada: alimentar() devuelve
    las cajas que se llenaron (con su número local, desde 1); cerrar()
    sella la cola como última caja de la razonada y REINICIA la numeración
    (la próxima razonada vuelve a CoT 1). Sin estados raros: el texto
    acumulado se conserva entero hasta que la razonada cierra."""

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


def cajas_del_turno(msgs):
    """Cajas COMPLETAS del turno (turno ya cerrado): cada razonada llena
    sus cajas al tope + cola, numeración desde 1 por razonada.
    → (cajas, rotulos) paralelos."""
    razonadas, _en_curso = _partes_del_turno(msgs)
    cajas, rotulos = [], []
    for texto in razonadas:
        for caja, n in cajas_de_texto(texto):
            cajas.append(caja)
            rotulos.append("CoT %d" % n)
    return cajas, rotulos


def cajas_vivas(msgs):
    """Ídem cajas_del_turno pero para el turno EN MARCHA: la razonada en
    curso solo aporta las cajas YA LLENAS (la que se llena ahora, no).
    → (cajas, rotulos, chars_en_curso, n_local_en_curso)."""
    razonadas, en_curso = _partes_del_turno(msgs)
    cajas, rotulos = [], []
    for texto in razonadas[:-1] if en_curso else razonadas:
        for caja, n in cajas_de_texto(texto):
            cajas.append(caja)
            rotulos.append("CoT %d" % n)
    if en_curso and razonadas:
        texto = razonadas[-1]
        llenas = len(texto) // COT_CAJA_CAP
        for caja, n in cajas_de_texto(texto[:llenas * COT_CAJA_CAP]):
            cajas.append(caja)
            rotulos.append("CoT %d" % n)
        pend = len(texto) % COT_CAJA_CAP
        return cajas, rotulos, pend, llenas + 1
    return cajas, rotulos, 0, 1


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


def botones_cajas(sid, t0, rotulos):
    """Un botón por caja; el RÓTULO reinicia por razonada (CoT 1 otra
    vez), pero el callback_data lleva el índice GLOBAL: ct:sid:t0:n."""
    return _filas([{"text": rotulos[i - 1][:36],
                    "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                   for i in range(1, min(len(rotulos), COT_BOTONES_MAX) + 1)])


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


def texto_vivo(titulo, n, k_actual=0, pend=0):
    """Estado de la caja de botones: cajas selladas + la que se llena."""
    linea = "🧠 <b>«%s»</b> pensando… · %d caja%s" % (
        esc(titulo or "?"), n, "s" if n != 1 else "")
    if pend:
        linea += " · llenando la CoT %d (%d chars)" % (k_actual, pend)
    if n:
        linea += "\nTocá un botón para leerlo:"
    return linea


def texto_final(titulo, t0, n, total):
    hora = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    texto = ("🧠 <b>Razonamiento</b> de «%s» (turno de las %s) — %d caja%s, "
             "%d chars. Tocá una para leerla:"
             % (esc(titulo or "?"), hora, n, "s" if n != 1 else "", total))
    if n > COT_BOTONES_MAX:
        texto += ("\n(botones para las primeras %d; el documento completo "
                  "con /cot)" % COT_BOTONES_MAX)
    return texto


# ------------------------------------------------------------- fuentes

def capturar(cfg, sid, titulo, t0, ws):
    """F40 — fallback al cerrar un turno de la Mac (si el vivo no pudo):
    trae el historial, cachea las cajas y manda el mensaje con botones.
    Devuelve la cantidad de cajas (None si no hubo razonamiento o falló)."""
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
        return None
    guardar(sid, t0, titulo, cajas, rotulos)
    total = sum(len(b) for b in cajas)
    ph = uhttp.send(cfg, texto_final(titulo or sid[-8:], t0, len(cajas),
                                      total), loud=False,
                    markup={"inline_keyboard": botones_cajas(sid, t0,
                                                             rotulos)})
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


def vivir(cfg, sid, titulo, t0, ws):
    """F41 — el vigilante del CoT de la Mac en vivo: sondea el historial
    cada 3s, y cada CAJA QUE SE LLENA agrega su botón (la caja de botones
    NACE con la primera caja sellada — nada de mensajes vacíos). Fin:
    marker consumido por on_stop (= el turno cerró) o techo de 35 min →
    texto final con el total. El cache se escribe al ARRANCAR: on_stop
    salta el --cot-botones de F40 si existe (el vigilante es el dueño)."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    guardar(sid, t0, titulo, [], [], vivo=True)  # dueño: F40 no duplica
    ph = None
    c = zc_proto.make(cwd=ws)
    try:
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
        if e := zc_proto.proto_err(r, "resume"):
            pwlog("cot-vivo %s: %s" % (sid[-8:], e[:120]))
            guardar(sid, t0, titulo, [], [], finalizado=True, error=e[:120])
            return
        visto = -1
        t_fin = time.time() + COT_VIVO_MAX_S
        ph = None  # F43c: el mensaje de las cajitas — el tap edita ACÁ
        while time.time() < t_fin and os.path.exists(marker_path(sid)):
            msgs = _fetch_messages(c, sid, limit=COT_MSGS) or []
            cajas, rotulos, pend, k = cajas_vivas(msgs)
            if len(cajas) != visto:
                guardar(sid, t0, titulo, cajas, rotulos, vivo=True,
                        **({"ph": ph} if ph else {}))
                texto = texto_vivo(titulo, len(cajas), k, pend)
                kb = {"inline_keyboard": botones_cajas(sid, t0, rotulos)}
                if ph:
                    uhttp._live_edit(cfg, ph, texto, markup=kb)
                else:
                    ph = uhttp.send(cfg, texto, loud=False, markup=kb)
                    if ph:
                        guardar(sid, t0, titulo, cajas, rotulos, vivo=True,
                                ph=ph)  # F43c: el tap edita este mensaje
                visto = len(cajas)
            time.sleep(COT_VIVO_CAD_S)
        # turno cerrado: un fetch más y el texto final con TODAS las cajas
        msgs = _fetch_messages(c, sid, limit=COT_MSGS) or []
        cajas, rotulos = cajas_del_turno(msgs)
        guardar(sid, t0, titulo, cajas, rotulos, finalizado=True)
        total = sum(len(b) for b in cajas)
        if ph:
            uhttp._live_edit(cfg, ph, texto_final(titulo, t0, len(cajas),
                                                  total),
                             markup={"inline_keyboard":
                                     botones_cajas(sid, t0, rotulos)})
        elif cajas:
            ph = uhttp.send(cfg, texto_final(titulo, t0, len(cajas), total),
                            loud=False,
                            markup={"inline_keyboard":
                                    botones_cajas(sid, t0, rotulos)})
        pwlog("cot-vivo fin: cajas=%d" % len(cajas))
    finally:
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
