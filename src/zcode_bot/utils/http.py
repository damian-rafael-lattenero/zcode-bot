"""HTTP de Telegram (síncrono) — cuerpos canónicos (consolidación F1e:
el gateway definía estas mismas funciones y pisaba este módulo; UN
cuerpo vivo). El cliente async para el webhook vive en http_client.py.

Los paths y la config se leen por ATRIBUTO (config.API / load_config de
zcode_bot.config) en call-time: fixture-aware en tests. `send` usa el
`cfg` recibido (la copia vieja de este módulo lo ignoraba y re-leía
config 3 veces — divergencia clasificada en el audit del 15/10).
"""
import json
import time
import urllib.request

from zcode_bot import config
from zcode_bot import limites as lim
from zcode_bot import audit
from zcode_bot.config import configured, load_config
from zcode_bot.utils.logging import log


def tg_api(cfg, method, params=None, timeout=35):
    """Llamada a Telegram Bot API."""
    token = cfg.get("token") or ""
    url = f"{config.API}/bot{token}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# F8 (incidente 15:47): el streaming en vivo de un turno pesado saturó el
# rate limit del bot y los reintentos a ciegas MANTUVIERON el throttle
# vivo (429 seguidos durante minutos, cajas perdidas). Desde acá: el
# primer 429 fija la ventana que Telegram pide (parameters.retry_after)
# y todo sender best-effort SE RETIRA hasta que pase — golpear más
# extiende el castigo. `send` (respuesta final) sí espera de verdad.
_cooldown_until = [0.0]
_dormir = time.sleep


def _retry_after(exc):
    """Segundos que el 429 pide esperar (body parameters.retry_after), o
    None si no es un 429. HTTPError trae el body en exc.read()."""
    if "429" not in str(exc) and "Too Many" not in str(exc):
        return None
    try:
        d = json.loads(exc.read().decode()) or {}
        return max(int((d.get("parameters") or {}).get("retry_after") or 0), 1)
    except Exception:  # noqa: BLE001 - 429 sin body legible: conservador
        return 5


def _message_id(r):
    """message_id de la respuesta de un sendMessage (None si no vino).
    bool es subclase de int: True NO es un id."""
    try:
        mid = (r.get("result") or {}).get("message_id")
    except AttributeError:
        return None
    return mid if isinstance(mid, int) and not isinstance(mid, bool) else None


def send(cfg, text, loud=None, markup=None, a_outbox=True):
    """Envía un mensaje con reintentos; devuelve el message_id si salió
    (True si Telegram no lo trajo — siempre truthy), False si no. Nunca
    lanza. El id es lo que permite EDITAR después ese mensaje (teclado
    del CoT que crece): devolver un True pelado dejó las cajas de botones
    clavadas en «CoT 1» — editMessageText(message_id=True) falla mudo.

    loud=None → suena solo si loud_all está activo; loud=True/False fuerza.
    markup → dict reply_markup (botones inline), ej {"inline_keyboard": [...]}.
    a_outbox=False → sin re-encolar al fallar (outbox_flush ya lo tiene)."""
    if not configured(cfg):
        return False
    if loud is None:
        loud = bool(cfg.get("loud_all"))  # divergencia F34c: era fallback False
    params = {
        "chat_id": cfg["chat_id"],
        "text": text[:lim.CH_RESPUESTA],
        "parse_mode": "HTML",
        "disable_notification": not loud,
    }
    if markup:
        params["reply_markup"] = json.dumps(markup)
    delays = (0, 1, 3)  # resets de red puntuales no pierden la notificación
    for attempt, delay in enumerate(delays, 1):
        if delay:
            _dormir(delay)
        try:
            r = tg_api(cfg, "sendMessage", params)
            return _message_id(r) or True
        except Exception as exc:  # noqa: BLE001 - un hook jamás debe romper la sesión
            ra = _retry_after(exc)
            if ra is not None:
                # 429: esperar el retry_after REAL entre intentos — los
                # 1s/3s de red no sirven contra el throttle de Telegram
                _cooldown_until[0] = time.time() + ra
                if attempt < len(delays):
                    _dormir(min(ra + 0.5, 60))
                continue
            log("errors", f"sendMessage falló (intento {attempt}/{len(delays)}): {exc}")
    if a_outbox:
        # F30: fallo FINAL de envío → registro global (el auditor correlaciona
        # ráfagas: el incidente 429/deadlock de las 15:47 habría alertado acá)
        audit.registrar(None, "envio", "sendMessage caído, va al outbox")
        _outbox_put(text)  # red caída: persistir y que el poller reintente
    return False


def _live_send(cfg, text):
    """sendMessage que devuelve message_id (para el placeholder en vivo)."""
    if time.time() < _cooldown_until[0]:
        return None  # en throttle: la caja nace muerta — jamás crítica
    try:
        r = tg_api(cfg, "sendMessage",
                   {"chat_id": cfg["chat_id"], "text": text[:lim.CH_RESPUESTA],
                    "parse_mode": "HTML", "disable_notification": True})
        return _message_id(r)
    except Exception:  # noqa: BLE001
        return None


def _live_edit(cfg, msg_id, text, markup=None):
    """editMessageText best-effort: el en vivo es optativo, un fallo nunca
    rompe nada. Con 429 fija el cooldown global y SIN reintentar: mientras
    dure, los edits se saltan solos (el streaming se degrada, no golpea).
    markup: teclado inline a la vez que el texto (F41: el teclado del CoT
    en vivo CRECE por edición, sin spam de mensajes)."""
    if time.time() < _cooldown_until[0]:
        return False
    try:
        params = {"chat_id": cfg["chat_id"], "message_id": msg_id,
                  "text": text[:lim.CH_RESPUESTA], "parse_mode": "HTML"}
        if markup is not None:
            params["reply_markup"] = markup
        tg_api(cfg, "editMessageText", params)
        return True
    except Exception as exc:  # noqa: BLE001
        ra = _retry_after(exc)
        if ra is not None:
            _cooldown_until[0] = time.time() + ra
        return False


def _multipart_body(fields, fname, content):
    """Cuerpo multipart/form-data (stdlib puro) para sendDocument."""
    import uuid
    b = "----zcb" + uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
                 "%s\r\n" % (b, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; "
             "filename=\"%s\"\r\nContent-Type: text/plain\r\n\r\n"
             % (b, fname)).encode()
    body += content + ("\r\n--%s--\r\n" % b).encode()
    return body, "multipart/form-data; boundary=%s" % b


def send_document(cfg, filename, text, caption=""):
    """Manda un .txt como documento de Telegram: el CoT completo de un
    turno no entra en mensajes de 4096 chars — el visor del documento sí
    lo scrollea entero (sendDocument, core.telegram.org/bots/api)."""
    if not configured(cfg):
        return False
    try:
        body, ctype = _multipart_body(
            {"chat_id": str(cfg["chat_id"]), "caption": caption[:900]},
            filename, text.encode("utf-8"))
        req = urllib.request.Request(
            "%s/bot%s/sendDocument" % (config.API, cfg.get("token") or ""),
            data=body, headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=90) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as exc:  # noqa: BLE001 - nunca romper el turno
        log("errors", "sendDocument: %s" % exc)
        return False


def _outbox_store():
    """AtomicStore del outbox — path resuelto EN EL MOMENTO (fixture-aware)."""
    from zcode_bot.state import AtomicStore
    import os
    return AtomicStore(os.path.join(config.STATE_DIR, "outbox.json"))


def _outbox_put(text):
    """Encola un mensaje para cuando la red vuelva. Multi-proceso (hooks +
    poller escriben): read-modify-write bajo flock."""
    def add(box):
        box = box if isinstance(box, list) else []
        box.append({"ts": int(time.time()), "text": text[:lim.CH_RESPUESTA]})
        return box[-20:]
    try:
        _outbox_store().update_cross_process(add, [])
    except OSError as e:
        # flock deadline vencido (patológico): el mensaje se pierde del
        # outbox pero send ya lo intentó 3 veces — degradar con log
        log("errors", "outbox_put: %s" % e)


def outbox_flush(cfg):
    """Reenvía lo acumulado cuando la red volvió. Devuelve cantidad enviada.

    Incidente 15:47: esto enviaba DENTRO del flock y el send fallido
    re-encolaba con OTRO flock del mismo archivo → auto-deadlock (el flock
    no es reentrante entre fds) → poller congelado 30+ min. Ahora el lock
    solo cubre el SWAP (rápido): los sends van FUERA, los fallidos
    vuelven a la cola al final; a_outbox=False evita el re-encolado
    anidado (ya están en la cola: los conserva el put_back)."""
    store = _outbox_store()
    tomados = [None]

    def swap(box):
        tomados[0] = box if isinstance(box, list) else []
        return []

    try:
        store.update(swap, [])
    except OSError as e:
        log("errors", "outbox_flush swap: %s" % e)
        return 0
    ok = 0
    restan = []
    for item in tomados[0]:
        if send(cfg, item.get("text", ""), loud=False, a_outbox=False):
            ok += 1
        else:
            restan.append(item)
    if restan:
        def put_back(box):
            box = box if isinstance(box, list) else []
            return restan + box
        try:
            store.update(put_back, [])
        except OSError as e:
            log("errors", "outbox_flush put_back (%d perdidos): %s"
                % (len(restan), e))
    return ok
