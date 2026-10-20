"""updates — el plumbing de updates de Telegram (F4h).

Offset atómico, busy-flag de esperas, normalización de callbacks a
pseudo-mensajes, long-poll, filtro de dueño fail-closed y la housekeeping
del poller. El pipeline único: polling y webhook comparten normalize→
dispatcher (ADR-011). Transporte POR ATRIBUTO (uhttp.tg_api).
"""
import json
import os
import time

from zcode_bot import config
from zcode_bot.core import _questions
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.validation import first
from zcode_bot.utils.logging import log, pwlog, _log_rotate
from zcode_bot.utils.config import load_config


def busy_path():
    return os.path.join(config.STATE_DIR, "tg-busy")


# Un .log.1 ya fue rotado FUERA del camino (diagnóstico de un incidente
# cerrado); si encima pesa más que esto no es un log, es un flood — el
# errors.log.1 de 294MB del incidente 19/10 (3.1M tracebacks idénticos
# en ~5min) no tenía por qué sobrevivir a su propia housekeeping.
LOG_ROTADO_CAP_BYTES = 50 * 1024 * 1024


def _logs_rotados_gordos():
    """Purga los *.log.1 sobre el cap: la rotación por escritura (log())
    acota el flood en vivo, pero el .1 que deja atrás tampoco puede
    quedarse sin techo."""
    import glob
    n = 0
    for ruta in glob.glob(os.path.join(config.LOG_DIR, "*.log.1")):
        try:
            if os.path.getsize(ruta) > LOG_ROTADO_CAP_BYTES:
                os.remove(ruta)
                n += 1
        except OSError:
            continue
    return n


def busy_fresh(max_age=None):
    """True si hay una espera de permiso/pregunta activa (el poller no debe
    tocar getUpdates). D2: la ventana deriva de wait_timeout_s (+60 de margen)
    — antes era 120 fijo con waits de 240: ventana de carrera del offset."""
    if max_age is None:
        max_age = int(load_config().get("wait_timeout_s", 240)) + 60
    try:
        return time.time() - os.path.getmtime(busy_path()) < max_age
    except OSError:
        return False


def current_offset():
    try:
        with open(os.path.join(config.STATE_DIR, "tg-offset")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def save_offset(value):
    # atómico (tmp + fsync + rename): un crash a mitad de escritura dejaba
    # el offset truncado → ValueError → offset 0 → re-procesamiento masivo
    # (mismo patrón que AtomicStore)
    os.makedirs(config.STATE_DIR, exist_ok=True)
    path = os.path.join(config.STATE_DIR, "tg-offset")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _cb_text(data):
    """callback_data de un botón inline → texto equivalente: el bot lo trata
    como si el dueño lo hubiera tipeado (/task n o @sid, número de opción,
    si/no). task:N es el vocabulario viejo (posición, ambiguo entre
    workspaces); t:sess_... identifica la conversación sin ambigüedad."""
    if data.startswith("t:ses"):  # t:sess_... o t:ses_... (formato viejo)
        return "/task @" + data[2:]
    if data.startswith("s:ses"):  # botón de stop
        return "/stop @" + data[2:]
    if data.startswith("c:ses"):  # botón de compactación
        return "/compact @" + data[2:]
    if data.startswith("mo:"):  # botón de modo (Plan mode / Full access)
        return "/mode " + data[3:]
    if data.startswith("qgo:"):  # F22: lanzar YA un encolado
        return "/qgo " + data[4:]
    if data.startswith("qdel:"):  # F22: descartar un encolado
        return "/qdel " + data[5:]
    if data == "retry":  # F22: botón 🔁 tras fallo/crash
        return "/retry"
    if data.startswith("m:"):  # botón de modelo
        return "/model " + data[2:]
    if data.startswith("th:"):  # botón de thinking
        return "/thinking " + data[3:]
    if data.startswith("task:"):
        # botones viejos (posición de la lista completa): resolver en all
        return "/all_tasks " + data[5:]
    if data.startswith("ans:"):
        return data[4:]
    if data == "yes":
        return "si"
    if data == "no":
        return "no"
    if data == "nuc:go":  # confirmación del botón ☢️ (dos pasos)
        return "/nuclear go"
    if data == "nuc:no":
        return "/nuclear no"
    if data == "dep:go":  # confirmación del botón 🚀 (dos pasos)
        return "/deploy go"
    if data == "dep:no":
        return "/deploy no"
    if data.startswith("df:"):  # F38b: archivo n del último commit (+hash [+parte])
        return "/diff " + data[3:].replace(":", " ")
    if data.startswith("dx:"):  # F38b: archivo n de los cambios sin commit [+parte]
        return "/diff dirty " + data[3:].replace(":", " ")
    if data.startswith("dc:"):  # F38e: un commit (lista de archivos con botones)
        return "/diff commit " + data[3:]
    if data == "dl":  # F38e: historial
        return "/diff log"
    if data == "dw":  # F38e: trabajo en curso
        return "/diff"
    return data[:64]


def answer_callback(cfg, cb):
    """answerCallbackQuery best-effort: corta el spinner del botón tocado."""
    try:
        uhttp.tg_api(cfg, "answerCallbackQuery", {"callback_query_id": cb.get("id") or ""})
    except Exception as exc:  # noqa: BLE001
        log("errors", f"answerCallbackQuery: {exc}")


def react(cfg, msg, emoji):
    """F23 — setMessageReaction (Bot API 7.0+): feedback en el mensaje
    ORIGINAL sin mandar ni un mensaje. 👀 al recibir, ✅ al entregar.
    Best-effort total: si el server no lo soporta, silencio."""
    mid = (msg or {}).get("message_id")
    if not mid:
        return
    try:
        uhttp.tg_api(cfg, "setMessageReaction",
               {"chat_id": cfg["chat_id"], "message_id": mid,
                "reaction": [{"type": "emoji", "emoji": emoji}]})
    except Exception:  # noqa: BLE001
        pass


def typing(cfg, seconds=5):
    """F23 — sendChatAction 'typing': mata la ventana muda entre el
    mensaje del dueño y el placeholder del worker. Best-effort."""
    try:
        uhttp.tg_api(cfg, "sendChatAction",
               {"chat_id": cfg["chat_id"], "action": "typing"})
    except Exception:  # noqa: BLE001
        pass


def normalize_update(cfg, u):
    """Update crudo de Telegram → mensaje del flujo interno (o None si el
    update no nos interesa). La MISMA traducción para polling y webhook
    (F3): botones inline → pseudo-mensaje con texto equivalente (_cb_text)
    — todo lo downstream (comandos, wait_reply, preguntas) funciona sin
    cambios. Estampa update_id (dedup del dispatcher en webhook)."""
    cb = u.get("callback_query")
    if cb:
        answer_callback(cfg, cb)
        data = cb.get("data") or ""
        pseudo = dict(cb.get("message") or {})
        if cb.get("from"):
            pseudo["from"] = cb["from"]
        pseudo["text"] = _cb_text(data)
        pseudo["_cb_data"] = data
        pseudo["update_id"] = u.get("update_id")
        return pseudo
    msg = u.get("message") or u.get("edited_message")
    if msg and msg.get("text"):
        msg = dict(msg)
        msg["update_id"] = u.get("update_id")
        return msg
    return None


def fetch_updates(cfg, offset, timeout):
    res = uhttp.tg_api(cfg, "getUpdates", {"offset": offset, "timeout": timeout, "limit": 100},
                 timeout=timeout + 15)
    updates = res.get("result", [])
    messages = []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        m = normalize_update(cfg, u)
        if m is not None:
            messages.append(m)
    return offset, messages, len(updates)


def drain_updates(cfg):
    """Consume mensajes pendientes y devuelve (offset_final, mensajes)."""
    offset = current_offset()
    messages = []
    for _ in range(10):
        try:
            offset, batch, n = fetch_updates(cfg, offset, 0)
        except Exception as exc:  # noqa: BLE001
            log("errors", f"getUpdates falló: {exc}")
            return offset, messages
        messages.extend(batch)
        if n < 100:
            return offset, messages
    return offset, messages


def from_owner(cfg, msg):
    """Fail-closed: sin owner_user_id en config NADIE pasa. El registro se
    hace por CLI (zcode-telegram.py register), nunca aceptando el primer
    mensaje que llegue — antes un config sin owner dejaba operar al bot a
    cualquiera que le escribiera."""
    owner = cfg.get("owner_user_id")
    if owner is None:
        log("errors", "from_owner: config sin owner_user_id — update "
                      "descartado (fail-closed; registrar con "
                      "`zcode-telegram.py register`)")
        return False
    return msg.get("from", {}).get("id") == owner


def _hq_pending():
    return _questions().any_pending()


def _reload_now_path():
    return os.path.join(config.STATE_DIR, config.ARCH_RELOAD)


def housekeeping():
    """F24 — purga de lo que se acumula sin techo (auditoría 13/10:
    23 backups sin purga, 964 payload-*.json con prompts en claro, 178
    proto-stderr vacíos, q-*.json huérfanos, markers viejos). Corre UNA
    vez al arrancar el poller. Todo best-effort."""
    import glob as _glob
    borrados = 0
    ahora = time.time()

    def viejos(pattern, dias, vacios=False):
        n = 0
        for ruta in _glob.glob(pattern):
            try:
                if vacios and os.path.getsize(ruta) > 0:
                    continue
                if ahora - os.path.getmtime(ruta) > dias * 86400:
                    os.remove(ruta)
                    n += 1
            except OSError:
                continue
        return n

    # backups: keep últimos 10
    try:
        bks = sorted(g for g in _glob.glob(
            os.path.join(config.BASE, "hooks", "backups", "*")) if os.path.isdir(g))
        for p in bks[:-10]:
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            borrados += 1
    except OSError:
        pass
    borrados += viejos(os.path.join(config.LOG_DIR, "payload-*.json"), 2)
    borrados += viejos(os.path.join(config.LOG_DIR, "proto-stderr-*.log"), 1,
                       vacios=True)
    borrados += viejos(os.path.join(config.STATE_DIR, "bot-q", "*.json"), 0.1)
    borrados += viejos(os.path.join(config.stop_dir(), "*"), 0.5)
    borrados += viejos(os.path.join(config.STATE_DIR, "turn-*.marker"), 1)
    borrados += _rollout_viejos(ahora)
    _log_rotate(os.path.join(config.LOG_DIR, "errors.log"))
    _log_rotate(os.path.join(config.LOG_DIR, "orphan.log"))
    borrados += _logs_rotados_gordos()
    if borrados:
        pwlog("housekeeping: %d artefactos purgados" % borrados)


def _rollout_viejos(ahora):
    """F9 — transcriptos de sesiones que el bot ya no referencia (rotadas/
    archivadas): cada rotación deja el model-io-<sid>.jsonl atrás y crece
    sin techo (44MB medidos por rotación). Se purgan los que llevan más de
    `rollout_retain_dias` (default 7, 0 = off) sin ser: sesión activa
    (bot-session.json), con turno activo (bot-active.json) o worker vivo
    (bot-workers.json). La sesión archivada sigue accesible en la app;
    perder el transcripto solo significa que su resume ya no repite el
    historial completo."""
    try:
        retener = float(load_config().get("rollout_retain_dias"))
    except (TypeError, ValueError):
        retener = 7.0
    if retener <= 0:
        return 0
    vivos = set()
    for nombre, por_clave in ((config.ARCH_SESION, False),
                              ("bot-active.json", True),
                              (config.ARCH_WORKERS, True)):
        try:
            with open(os.path.join(config.STATE_DIR, nombre)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        if por_clave:
            vivos.update(k for k in d
                         if isinstance(k, str) and k.startswith("ses"))
        elif isinstance(d.get("sid"), str):
            vivos.add(d["sid"])
    n = 0
    import glob as _glob
    corte = ahora - retener * 86400
    for ruta in _glob.glob(os.path.join(config.BASE, "cli", "rollout",
                                        "model-io-*.jsonl")):
        sid = os.path.basename(ruta)[len("model-io-"):-len(".jsonl")]
        if sid in vivos:
            continue
        try:
            if os.path.getmtime(ruta) > corte:
                continue
            os.remove(ruta)
            n += 1
        except OSError:
            continue
    return n
