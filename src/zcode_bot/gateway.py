#!/usr/bin/env python3
"""ZCode → Telegram remote monitor.

Notifica a Telegram todo lo que pasa en ZCode (turno iniciado/terminado,
permisos, preguntas, fallos, sesión trabada, app cerrada) y permite responder
permisos/preguntas desde el móvil. El pedido de permiso es una carrera: si
respondés desde Telegram gana el hook; si no, queda el diálogo de la app.

Setup (una vez):
  1. Crear un bot en @BotFather (ej: glm_lattenero_ctrl_bot) y copiar el token.
  2. zcode-telegram.py set-token <TOKEN>
  3. Mandarle cualquier mensaje al bot nuevo desde Telegram.
  4. zcode-telegram.py register        # aprende tu chat_id y confirma
  5. Reiniciar ZCode para que carguen los hooks.

Uso:
  hook                    (uso interno; lee el payload del hook por stdin)
  set-token <TOKEN>
  register                (espera un mensaje tuyo y bloquea el chat)
  test [texto]
  quota                   (informa y envía la quota z.ai al 2_bot)
  watch-app               (uso interno; loop del LaunchAgent)
  watch-commands          (uso interno; atiende /quota /ping /help del 2_bot)
  --stuck-watch <token> <session_id>   (uso interno)
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import unicodedata
import urllib.request

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".zcode")
CONFIG_PATH = os.environ.get("ZT_CONFIG", os.path.join(BASE, "telegram-remote.json"))
STATE_DIR = os.path.join(BASE, "hooks", "state")
LOG_DIR = os.path.join(BASE, "hooks", "logs")
API = os.environ.get("ZT_API_BASE", "https://api.telegram.org")

# Paquete núcleo (F2): state atómico, concurrencia con lock disciplinado,
# factory de protocolo, scanner de logs y repository sqlite.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zcode_bot import state as zc_state        # noqa: E402
from zcode_bot import concurrency as zc_conc   # noqa: E402
from zcode_bot import proto as zc_proto        # noqa: E402
from zcode_bot import cli_logs as zc_logs      # noqa: E402
from zcode_bot import tasks_repo as zc_repo    # noqa: E402
from zcode_bot import commands as zc_commands  # noqa: E402
from zcode_bot import reports as zc_reports    # noqa: E402

DEFAULTS = {
    "token": "",
    "chat_id": None,
    "owner_user_id": None,
    "owner_name": "",
    "wait_timeout_s": 240,
    "question_wait_timeout_s": 60,  # preguntas (modo phone): si no respondés del móvil en N s, aparece el diálogo en la Mac
    # 'mac': diálogo en la Mac inmediato + aviso ❓ al móvil (desde el teléfono se
    # responde por el bot integrado). 'phone': el hook retiene el diálogo esperando
    # la respuesta del móvil durante question_wait_timeout_s.
    "question_mode": "mac",
    "stuck_after_min": 20,
    "loud_all": True,
    # Errores de herramienta que no se notifican: el agente los corrige solo
    # (falla → lee/reintenta). Regex case-insensitive sobre el texto del error.
    "suppress_error_patterns": [
        "File has not been read yet",
        "File does not exist",
        "File has been (?:unexpectedly )?modified",
        "String to replace not found",
        "Found \\d+ matches? of the string to replace",
    ],
    "notify_prompt": True,
    "notify_stop": True,
    "notify_failures": True,
    "notify_session": True,
    # F15: el push no solicitado (hooks ▶️/✅/🔐/❓/❌/🟢, CoT, stuck-watch)
    # llega SOLO de la task current. Kill switch: false = todo notifica.
    "only_active_task": True,
    "enabled": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def log(kind, msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(LOG_DIR, f"{kind}.log"), "a") as f:
            f.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def log_payload(event, payload):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(LOG_DIR, f"payload-{stamp}-{event}.json"), "w") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    except (OSError, TypeError):
        pass


def tg_api(cfg, method, params=None, timeout=35):
    token = cfg.get("token") or ""
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def configured(cfg):
    return bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))


def send(cfg, text, loud=None, markup=None):
    """Envía un mensaje con reintentos; devuelve True si salió. Nunca lanza.

    loud=None → suena solo si loud_all está activo; loud=True/False fuerza.
    markup → dict reply_markup (botones inline), ej {"inline_keyboard": [...]}.
    """
    if not configured(cfg):
        return False
    if loud is None:
        loud = bool(cfg.get("loud_all", False))
    params = {
        "chat_id": cfg["chat_id"],
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_notification": not loud,
    }
    if markup:
        params["reply_markup"] = json.dumps(markup)
    delays = (0, 1, 3)  # resets de red puntuales no pierden la notificación
    for attempt, delay in enumerate(delays, 1):
        if delay:
            time.sleep(delay)
        try:
            tg_api(cfg, "sendMessage", params)
            return True
        except Exception as exc:  # noqa: BLE001 - un hook jamás debe romper la sesión
            log("errors", f"sendMessage falló (intento {attempt}/{len(delays)}): {exc}")
    _outbox_put(text)  # red caída: persistir y que el poller reintente
    return False


def _live_send(cfg, text):
    """sendMessage que devuelve message_id (para el placeholder en vivo)."""
    try:
        r = tg_api(cfg, "sendMessage",
                   {"chat_id": cfg["chat_id"], "text": text[:3900],
                    "parse_mode": "HTML", "disable_notification": True})
        return (r.get("result") or {}).get("message_id")
    except Exception:  # noqa: BLE001
        return None


def _live_edit(cfg, msg_id, text):
    """editMessageText best-effort: el en vivo es optativo, un fallo nunca
    rompe nada. Con 429 (throttle de Telegram) espera y sigue."""
    try:
        tg_api(cfg, "editMessageText",
               {"chat_id": cfg["chat_id"], "message_id": msg_id,
                "text": text[:3900], "parse_mode": "HTML"})
        return True
    except Exception as exc:  # noqa: BLE001
        if "429" in str(exc) or "Too Many" in str(exc):
            time.sleep(5)
        return False


def _store(name):
    """AtomicStore para un archivo de state (path resuelto EN EL MOMENTO —
    así los tests pueden parchear STATE_DIR después del import)."""
    return zc_state.AtomicStore(os.path.join(STATE_DIR, name))


def _outbox_put(text):
    """Encola un mensaje para cuando la red vuelva. Multi-proceso (hooks +
    poller escriben): read-modify-write bajo flock."""
    def add(box):
        box = box if isinstance(box, list) else []
        box.append({"ts": int(time.time()), "text": text[:3900]})
        return box[-20:]
    try:
        _store("outbox.json").update_cross_process(add, [])
    except OSError:
        pass


def outbox_flush(cfg):
    """Reenvía lo acumulado cuando la red volvió. Devuelve cantidad enviada.
    El drain corre dentro del flock para no perder puts concurrentes."""
    ok = [0]

    def drain(box):
        restan = []
        for item in box if isinstance(box, list) else []:
            if send(cfg, item["text"], loud=False):
                ok[0] += 1
            else:
                restan.append(item)
        return restan
    try:
        _store("outbox.json").update_cross_process(drain, [])
    except OSError:
        pass
    return ok[0]


def task_label(payload):
    """Título legible de la tarea que originó el evento (como en la lista de la app)."""
    session = str(first(payload, "session_id", "sessionId") or "")
    title = ""
    try:
        import sqlite3
        db = os.path.join(BASE, "v2", "tasks-index.sqlite")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT title FROM tasks WHERE task_id = ? AND deleted = 0 LIMIT 1",
                (session,)).fetchone()
        finally:
            con.close()
        title = (row[0] or "").strip() if row else ""
    except Exception:  # noqa: BLE001 - etiqueta es best-effort
        title = ""
    if not title:
        cwd = str(payload.get("cwd") or "")
        title = os.path.basename(cwd.rstrip("/")) if cwd else "ZCode"
    return title[:60]


def labeled(payload, body):
    return f"🧵 <b>{esc(task_label(payload))}</b>\n{body}"


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


YES_EXACT = {"si", "yes", "y", "ok", "okay", "dale", "1", "acepto", "go", "apru", "apruébalo"}
NO_EXACT = {"no", "n", "nop", "nope", "2", "never", "núnca"}
YES_PREFIX = ("aprob", "allow", "permit", "acept", "autoriz")
NO_PREFIX = ("deny", "deneg", "rechaz", "bloque", "cancel", "nope")


def classify(text):
    """Devuelve 'yes' | 'no' | 'text'."""
    t = norm(text)
    if not t:
        return "text"
    first = re.sub(r"[!?.,;:¡¿]+$", "", t.split(" ", 1)[0])
    if first in YES_EXACT or t in YES_EXACT:
        return "yes"
    if first in NO_EXACT or t in NO_EXACT:
        return "no"
    if any(first.startswith(p) for p in YES_PREFIX):
        return "yes"
    if any(first.startswith(p) for p in NO_PREFIX):
        return "no"
    return "text"


# ---------------------------------------------------------------- respuestas

def busy_path():
    return os.path.join(STATE_DIR, "tg-busy")


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
        with open(os.path.join(STATE_DIR, "tg-offset")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def save_offset(value):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "tg-offset"), "w") as f:
        f.write(str(value))


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
    return data[:64]


def answer_callback(cfg, cb):
    """answerCallbackQuery best-effort: corta el spinner del botón tocado."""
    try:
        tg_api(cfg, "answerCallbackQuery", {"callback_query_id": cb.get("id") or ""})
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
        tg_api(cfg, "setMessageReaction",
               {"chat_id": cfg["chat_id"], "message_id": mid,
                "reaction": [{"type": "emoji", "emoji": emoji}]})
    except Exception:  # noqa: BLE001
        pass


def typing(cfg, seconds=5):
    """F23 — sendChatAction 'typing': mata la ventana muda entre el
    mensaje del dueño y el placeholder del worker. Best-effort."""
    try:
        tg_api(cfg, "sendChatAction",
               {"chat_id": cfg["chat_id"], "action": "typing"})
    except Exception:  # noqa: BLE001
        pass


def fetch_updates(cfg, offset, timeout):
    res = tg_api(cfg, "getUpdates", {"offset": offset, "timeout": timeout, "limit": 100},
                 timeout=timeout + 15)
    updates = res.get("result", [])
    messages = []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        cb = u.get("callback_query")
        if cb:
            # botón inline: entra al flujo como un mensaje tipeado con el
            # texto equivalente (ver _cb_text) — todo lo downstream (comandos,
            # wait_reply, preguntas) funciona sin cambios
            answer_callback(cfg, cb)
            data = cb.get("data") or ""
            pseudo = dict(cb.get("message") or {})
            if cb.get("from"):
                pseudo["from"] = cb["from"]
            pseudo["text"] = _cb_text(data)
            pseudo["_cb_data"] = data
            messages.append(pseudo)
            continue
        msg = u.get("message") or u.get("edited_message")
        if msg and msg.get("text"):
            messages.append(msg)
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
    owner = cfg.get("owner_user_id")
    return owner is None or msg.get("from", {}).get("id") == owner


def wait_reply(cfg, hint):
    """Espera una respuesta NUEVA por Telegram hasta wait_timeout_s.

    Devuelve ("yes"|"no"|"text", texto) o (None, None) si se agotó el tiempo.
    Los mensajes que empiezan con "/" son comandos del 2_bot: se responden y
    la espera continúa (no cuentan como respuesta).
    """
    deadline = time.time() + int(cfg.get("wait_timeout_s", 240))
    offset, stale = drain_updates(cfg)  # lo pendiente no cuenta como respuesta
    save_offset(offset)
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        while time.time() < deadline:
            # flag para el poller de comandos: no consumir getUpdates mientras esperamos
            with open(busy_path(), "w") as f:
                f.write(str(time.time()))
            remaining = int(deadline - time.time())
            poll_timeout = max(1, min(25, remaining))
            try:
                offset, batch, _ = fetch_updates(cfg, offset, poll_timeout)
                save_offset(offset)
            except Exception as exc:  # noqa: BLE001
                log("errors", f"getUpdates (wait) falló: {exc}")
                time.sleep(3)
                continue
            for msg in batch:
                if not from_owner(cfg, msg):
                    continue
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    reply = handle_command(cfg, text)
                    if reply is not None:
                        if isinstance(reply, tuple):
                            send(cfg, reply[0], loud=False, markup=reply[1])
                        else:
                            send(cfg, reply, loud=False)
                        continue
                kind = classify(text)
                return (kind if kind in ("yes", "no") else "text"), text
            if not batch:
                time.sleep(0.5)  # servidores que no bloquean: evita loop cerrado
        return None, None
    finally:
        try:
            os.remove(busy_path())
        except OSError:
            pass


# ---------------------------------------------------------------- comandos 2_bot

def fmt_tok(n):
    """15800 -> '15.8K' · 17400000 -> '17.4M'"""
    if n is None:
        return "?"
    n = float(n)
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}K"
    return str(int(n))


def fmt_num(n):
    """Para credits del panel: 880 -> '880' · 28830 -> '28.8K' (K recién desde 10K)"""
    if n is None:
        return "?"
    n = float(n)
    if n >= 1e4:
        v = n / 1e3
        return ("%.1fK" % v).replace(".0K", "K")
    return str(int(n))


def datetime_dt(epoch_s):
    """epoch → 'HH:MM' si es hoy, 'dd/mm HH:MM' si no."""
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch_s)
    if dt.date() == datetime.date.today():
        return dt.strftime("%H:%M")
    return dt.strftime("%d/%m %H:%M")


def session_titles():
    """taskId -> título, desde el índice de sesiones de la app."""
    import glob as globmod
    titles = {}
    for path in globmod.glob(os.path.join(BASE, "v2", "sessions", "*", "ses_*.json")):
        try:
            with open(path) as f:
                meta = json.load(f).get("meta") or {}
            if meta.get("taskId"):
                titles[meta["taskId"]] = meta.get("title") or ""
        except (OSError, ValueError):
            continue
    return titles


def coding_usage(day_offset=0):
    """Usage real del coding plan desde los registros model-io del CLI
    (~/.zcode/cli/rollout/model-io-*.jsonl): usage por request con valores
    reales (el log de texto los redacta). La retención es rolling, así que
    la ventana devuelta dice desde qué hora alcanza el dato.
    Devuelve (totales, {sessionId: {req, total}}, (desde, hasta)) o (None, None, None).
    """
    import datetime
    import glob as globmod
    target = datetime.date.today() - datetime.timedelta(days=day_offset)
    tot = {"req": 0, "in": 0, "out": 0, "cache": 0, "total": 0}
    per_sess = {}
    window = [None, None]
    for path in globmod.glob(os.path.join(BASE, "cli", "rollout", "model-io-*.jsonl")):
        try:
            f = open(path, errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "coding-plan" not in str((d.get("model") or {}).get("providerId") or ""):
                    continue
                ts = d.get("completedAt") or ""
                try:
                    local = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue
                if local.date() != target:
                    continue
                u = (d.get("response") or {}).get("usage") or {}

                def _num(k):
                    try:
                        return int(u.get(k) or 0)
                    except (TypeError, ValueError):
                        return 0
                window[0] = window[0] or local.strftime("%H:%M")
                window[1] = local.strftime("%H:%M")
                tot["req"] += 1
                tot["in"] += _num("inputTokens")
                tot["out"] += _num("outputTokens")
                tot["cache"] += _num("cacheReadTokens")
                tot["total"] += _num("totalTokens")
                sid = str(d.get("sessionId") or "?")
                s = per_sess.setdefault(sid, {"req": 0, "total": 0})
                s["req"] += 1
                s["total"] += _num("totalTokens")
    if not tot["req"]:
        return None, None, None
    return tot, per_sess, tuple(window)


def read_quota_cache():
    """Lee el snapshot de quota del coding plan que el renderer de la app
    cachea en su Local Storage (leveldb). Es el mismo dato que muestra el
    panel Usage de la app: limits con currentValue (usados), usage (límite),
    percentage y nextResetTime por ventana. El snapshot se reescribe en el
    .log vivo del leveldb como texto plano; buscamos la última aparición.
    Devuelve dict {level, windows:[{used,limit,remaining,pct,reset_ms}], mcp} o None.
    """
    import datetime
    ldir = os.path.expanduser("~/Library/Application Support/ZCode/session/Local Storage/leveldb")
    if not os.path.isdir(ldir):
        return None
    files = []
    for name in os.listdir(ldir):
        if name.endswith(".log") or name.endswith(".ldb"):
            p = os.path.join(ldir, name)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
    marker = b'"quota":{"level"'
    best = None  # (nextResetTime máximo visto → entrada más fresca)
    for _, path in sorted(files, reverse=True):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        pos = 0
        while True:
            i = data.find(marker, pos)
            if i < 0:
                break
            pos = i + 1
            # extraer el objeto JSON balanceado que arranca en "quota":
            start = i + len(b'"quota":')
            depth = 0
            end = None
            in_str = False
            esc = False
            for j in range(start, min(start + 8000, len(data))):
                c = data[j:j + 1]
                if esc:
                    esc = False
                    continue
                if c == b"\\":
                    esc = True
                    continue
                if c == b'"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c == b"{":
                    depth += 1
                elif c == b"}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            if not end:
                continue
            try:
                q = json.loads(data[start:end])
            except ValueError:
                continue
            if not isinstance(q, dict) or "limits" not in q:
                continue
            freshest = max((l.get("nextResetTime") or 0) for l in q["limits"])
            if best is None or freshest > best[0]:
                best = (freshest, q)
    if not best:
        return None
    q = best[1]
    windows = []
    for lim in q.get("limits") or []:
        if lim.get("type") != "CREDIT_LIMIT":
            continue
        windows.append({
            "hours": lim.get("number"),  # 5 → ventana 5h, 1 → semanal
            "used": lim.get("currentValue"),
            "limit": lim.get("usage"),
            "remaining": lim.get("remaining"),
            "pct": lim.get("percentage"),
            "reset_ms": lim.get("nextResetTime"),
        })
    windows.sort(key=lambda w: (w["hours"] or 0), reverse=True)  # 5h primero, semana después
    mcp = None
    agg = q.get("mcpAggregate") or q.get("mcp_aggregate") or {}
    if isinstance(agg, dict) and agg.get("limit"):
        mcp = {"used": agg.get("used") or 0, "limit": agg.get("limit")}
    return {"level": q.get("level"), "windows": windows, "mcp": mcp}


def _quota_api_key():
    """API key del coding plan desde bot-provider.json (nunca loguear)."""
    try:
        with open(os.path.join(BASE, "hooks", "bot-provider.json")) as f:
            cfg = json.load(f)
        for rule in ((((cfg.get("config") or {}).get("providerConfigRules")
                       or {}).get("providerRules")) or []):
            acc = (rule.get("config") or {}).get("access") or {}
            if acc.get("type") == "zhipu-coding-plan-api-key" and acc.get("apiKey"):
                return acc["apiKey"]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def quota_live():
    """Quota EN VIVO del endpoint del panel (Bearer con la API key del bot).
    Devuelve {"level","windows":[{hours,used,limit,pct,reset_ms}]} o None.
    No depende de que la app haya escrito su cache (el leveldb se compacta
    y el snapshot desaparece — pasó el 11/10)."""
    key = _quota_api_key()
    if not key:
        return None
    for intento in range(3):
        try:
            req = urllib.request.Request(
                "https://api.z.ai/api/monitor/usage/quota/limit",
                headers={"Authorization": "Bearer " + key,
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = (json.loads(r.read().decode()).get("data")) or {}
            out = {"level": data.get("level"), "windows": []}
            for w in data.get("limits") or []:
                used = w.get("currentValue")
                limit = w.get("usage")
                if not isinstance(used, (int, float)) or not limit:
                    continue
                out["windows"].append({
                    "hours": w.get("number"),
                    "used": used, "limit": limit,
                    "pct": w.get("percentage"),
                    "reset_ms": w.get("nextResetTime")})
            return out if out["windows"] else None
        except Exception as exc:  # noqa: BLE001 - red flaky: reintentar
            log("errors", "quota_live intento %d: %s" % (intento + 1, str(exc)[:120]))
            time.sleep(1 + intento)
    return None


def quota_report():
    """Coding plan: SOLO credits 5h + weekly con resets (como el panel Usage).
    Fuente primaria: endpoint en vivo; fallback: cache del panel de la app.
    El detalle de tokens por sesión vive en /usage."""
    lines = ["📊 <b>Coding plan</b>"]
    snap = quota_live() or read_quota_cache()
    if snap:
        nombres = {5: "⏱ 5 horas", 1: "📅 Semanal"}
        for w in snap["windows"]:
            nombre = nombres.get(w["hours"], "🔒 Límite")
            pct = f" ({w['pct']}%)" if w.get("pct") is not None else ""
            alerta = " ⚠️" if (w.get("pct") or 0) >= 75 else ""
            reset = ""
            if w.get("reset_ms"):
                reset = " · resetea " + datetime_dt(w["reset_ms"] / 1000)
            lines.append(f"{nombre}: {fmt_num(w['used'])}/{fmt_num(w['limit'])}"
                         f" credits{pct}{reset}{alerta}")
    else:
        lines.append("(sin datos — no pude ni al endpoint ni al cache)")
    lines.append("Detalle por sesión: /usage")
    return "\n".join(lines)


def usage_report():
    """Detalle del consumo de hoy por sesión (coding plan)."""
    tot, per_sess, window = coding_usage(0)
    if not tot:
        return "📈 Sin consumo registrado hoy todavía."
    lines = [f"📈 <b>Uso hoy por sesión</b> ({window[0]}–{window[1]})"]
    titles = session_titles()
    filas = sorted(per_sess.items(), key=lambda kv: -kv[1]["total"])
    for sid, s in filas[:8]:
        nombre = esc(titles.get(sid) or ("…" + sid[-10:]))
        lines.append(f"• {nombre}: {s['req']} req · {fmt_tok(s['total'])}")
    if len(filas) > 8:
        resto = sum(s["total"] for _, s in filas[8:])
        lines.append(f"• (+{len(filas) - 8} sesiones: {fmt_tok(resto)})")
    lines.append(f"<b>Total: {tot['req']} req · {fmt_tok(tot['total'])} tokens</b>")
    return "\n".join(lines)


# Ventana de contexto por modelo (tokens). GLM-5.3: 1M oficial (128K output).
CONTEXT_WINDOWS = {"GLM-5.3": 1000000, "GLM-5.3-Flash": 1000000}


def active_workspace_path():
    """Workspace que muestra la ventana de la app (setting.json:
    lastWorkspaceSession[lastActiveTabIndex])."""
    try:
        s = json.load(open(os.path.join(BASE, "v2", "setting.json")))
        lws = s.get("lastWorkspaceSession") or []
        i = s.get("lastActiveTabIndex")
        if lws and isinstance(i, int) and 0 <= i < len(lws) and isinstance(lws[i], dict):
            return lws[i].get("workspacePath")
        if lws and isinstance(lws[0], dict):
            return lws[0].get("workspacePath")
    except (OSError, ValueError):
        pass
    return None


def ctx_tasks(limit=10):
    """Tasks con consumo real, sin archivadas, en el orden de la barra
    lateral de la app (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.ctx_tasks(BASE, limit=limit)


def _compactiones(horas=48):
    """{sessionId: epoch} del último compact.completed por sesión (scanner
    Strategy: zcode_bot.cli_logs)."""
    lim = time.time() - horas * 3600
    out = {}
    for ts, sid, _ev, _d in zc_logs.iter_events(
            BASE, needles=("compact.completed",), since_epoch=lim,
            last_files=2, tail_bytes=0):
        out[sid] = ts
    return out


def _ctx_live(tasks):
    """Contexto REAL (proyección persistida) por sesión, vía subproceso
    desechable ctx_probe.py: un resume de una sesión gigante infla MEMORIA
    y no puede pasar en el poller (dos veces lo mató). Devuelve {sid: used}
    con lo que pudo medir; {} si el probe falla o se cuelga (timeout 90s)."""
    plan = {}
    for t in tasks:
        if t.get("ws"):
            plan.setdefault(t["ws"], []).append(t["sid"])
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "hooks", "ctx_probe.py"),
             json.dumps(plan)],
            capture_output=True, text=True, timeout=90)
        data = json.loads((r.stdout or "").strip() or "{}")
        return {sid: v["used"] for sid, v in data.items()
                if isinstance(v, dict) and "used" in v}
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        log("errors", "ctx_live fallo: %s" % str(exc)[:120])
        return {}


def context_report():
    """Contexto por task, numerado como la lista de /compact. El % viene de
    la proyección PERSISTIDA de cada sesión (el número que verá el próximo
    turno) — el del último turno queda como referencia entre paréntesis."""
    import datetime
    tasks = ctx_tasks(8)
    if not tasks:
        return "🧠 Sin tasks con uso registrado."
    act = active_workspace_path()
    now = datetime.datetime.now()
    comp = _compactiones()
    live = _ctx_live(tasks)
    lines = ["🧠 <b>Contexto por task</b> (estado real en disco)"]
    for t in tasks:
        window = CONTEXT_WINDOWS.get(t["model"], 1000000)
        real = live.get(t["sid"])
        ctx = real if real else t["ctx"]
        pct = ctx * 100.0 / window
        emoji = "🔴" if pct >= 80 else ("🟡" if pct >= 50 else "🟢")
        dt = datetime.datetime.fromtimestamp(t["when"] / 1000)
        horas = (now - dt).total_seconds() / 3600
        hace = "ahora" if horas < 0.2 else (f"hace {int(horas)}h" if horas < 24 else dt.strftime("%d/%m"))
        tag = "" if t["ws"] == act else " · 📁" + esc(os.path.basename(t["ws"] or ""))
        broom = ""
        if t["sid"] in comp:
            hc = datetime.datetime.fromtimestamp(comp[t["sid"]])
            broom = f" · 🧹 {hc.strftime('%H:%M')}"
        ref = "" if real else f" (últ. turno {fmt_tok(t['ctx'])})"
        lines.append(f"{t['n']}. {emoji} {fmt_tok(ctx)} ({pct:.0f}%){ref}{broom}{tag}"
                     f" — {esc(t['title'] or t['sid'][-8:])}")
    lines.append("\n/compact &lt;n&gt; compacta la task n (la app solo refleja"
                 " el compact al reabrir la task).")
    return "\n".join(lines)


def session_title_from_db(sid):
    """Título de una sesión (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.session_title(BASE, sid)


def compact_list_report(scope="projects"):
    """/compact → lista tocable (Template Method: zcode_bot.reports)."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    activos = running_turns()
    act = active_bot_session()

    def marca_de(t):
        return ("🔄" if t["sid"] in activos
                else ("🎯" if act and t["sid"] == act.get("sid") else "·"))

    def line_of(t, i):
        return "%s %d. %s — %s" % (marca_de(t), t["n"],
                                   esc(t["title"] or t["sid"][-8:]),
                                   fmt_tok(t["ctx"]))

    def button_of(t, i):
        return {"text": "%s %d · %s (%s)" % (marca_de(t), t["n"],
                                             (t["title"] or t["sid"][-8:])[:28],
                                             fmt_tok(t["ctx"])),
                "callback_data": "c:%s" % t["sid"]}

    return zc_reports.build_list_report(
        tasks,
        "🧹 <b>Compactar — tocá una</b> (solo idle)"
        + (" · /all_compact para todas" if scope == "all" else ""),
        "🧠 Sin tasks%s para compactar — /all_compact muestra todas."
        % ("" if scope == "all" else " de proyecto"),
        line_of, button_of,
        "🔄 no se puede (está corriendo) · 🎯 es la activa.")


def _compact_args(rest, scope):
    """/compact [<n|@sid>] [instrucciones] → zcode_compact."""
    n = sid = None
    resto = rest
    head = rest.split()[0]
    if head.startswith("@ses"):
        sid = head[1:]
        resto = rest.split(" ", 1)[1].strip() if " " in rest else ""
    elif head.isdigit():
        n = int(head)
        resto = rest.split(" ", 1)[1].strip() if " " in rest else ""
    return zcode_compact(resto, n=n, sid=sid, scope=scope)


def zcode_compact(rest="", n=None, sid=None, scope="projects"):
    """Compacta por el ZCode Protocol (headless: sin UI, sin permisos,
    funciona con la pantalla bloqueada). Target: sid (botones) > n (posición
    de la lista del scope) > conversación activa > task más reciente del
    proyecto activo. Solo sesiones idle. Snapshot/restore del model
    selection para no contaminar sesiones de la app."""
    import sys as _sys
    import time as _t
    if sid is not None:
        target = {t["sid"]: t for t in ctx_tasks(30)}.get(sid)
        if target is None:
            return "⚠️ No encontré esa conversación — mandá /compact."
    elif n is not None:
        tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
        if n < 1 or n > len(tasks):
            return ("⚠️ Número fuera de la lista (1 a %d) — mandá /compact."
                    % len(tasks))
        target = tasks[n - 1]
    else:
        act = active_bot_session()
        target = None
        if act and act.get("sid"):
            target = {t["sid"]: t for t in ctx_tasks(30)}.get(act["sid"])
        if target is None:
            aw = active_workspace_path()
            cands = [t for t in ctx_tasks(20) if t["ws"] == aw]
            if not cands:
                return ("🤔 No hay tasks con uso en el proyecto activo — "
                        "mandá /compact para elegir.")
            target = cands[0]
    sid, ws, titulo = target["sid"], target["ws"], target["title"]
    try:
        c = zc_proto.make(cwd=ws)
    except OSError as e:
        return "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120])
    snap_sel = None
    try:
        r = c.request("session/list", {}, timeout=30)
        if "error" in r:
            return "❌ session/list: " + esc(json.dumps(r["error"])[:150])
        st = {x["sessionId"]: x.get("status")
              for x in (r.get("result") or {}).get("sessions", [])}
        if st.get(sid) == "running":
            return ("⚠️ «%s» está corriendo — frená primero (/stop o el botón "
                    "Stop) y reintentá." % esc(titulo))
        r = c.request("session/resume", {"sessionId": sid}, timeout=60)
        if "error" in r:
            return "❌ resume: " + esc(json.dumps(r["error"])[:150])
        snap_sel = _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": MODEL_SEL}, timeout=30)
        if "error" in r:
            return "❌ setModel: " + esc(json.dumps(r["error"])[:150])
        params = {"sessionId": sid}
        if rest:
            params["instructions"] = rest
        started = _t.time()
        r = c.request("session/compact", params, timeout=300)
        if "error" in r:
            return "❌ compact: " + esc(json.dumps(r["error"])[:150])
        fin = _wait_cli_event(sid, started,
                              ("compact.completed", "compact.failed"), 200)
        if fin is None:
            return ("🤔 Compactación enviada pero no vi el evento de fin en "
                    "200 s. Verificá con /ctx.")
        if fin.get("event") == "compact.failed":
            err = str((fin.get("error") or {}).get("message", ""))[:120]
            return "❌ La compactación de «%s» falló: %s" % (esc(titulo), esc(err))
        # el compact achica el replay de session/events: rehabilitar el
        # CoT en vivo para los próximos turnos de esta sesión
        _live_skip_clear(sid)
        dur = (fin.get("durationMs") or 0) / 1000.0
        if dur < 2:
            return ("🧹 «%s» compactada (al instante: no había nada nuevo "
                    "que resumir). Verificá con /ctx." % esc(titulo))
        # números REALES: la compactación deja un part "compaction" en el
        # historial con preCompactTokenCount/postCompactTokenCount exactos
        # (mejor que estimar desde model_usage)
        pre = post = None
        try:
            r = c.request("session/messages", {"sessionId": sid, "limit": 15},
                          timeout=30)
            for m in (r.get("result") or {}).get("messages") or []:
                for p in m.get("parts", []):
                    if p.get("type") == "compaction" and p.get("operationId"):
                        pre = p.get("preCompactTokenCount")
                        # truePost = contexto REAL restante; postCompact puede
                        # ser MAYOR que pre (conteo sin recorte) y confundir
                        post = (p.get("truePostCompactTokenCount")
                                or p.get("postCompactTokenCount"))
        except Exception:  # noqa: BLE001
            pass
        nums = ""
        if pre and post:
            nums = " Contexto: %s → %s." % (fmt_tok(pre), fmt_tok(post))
        return ("🧹 Compactación de «%s» lista en %.0fs.%s\n"
                "⚠️ Si esta task está abierta en la app, reabrila — su ventana "
                "sigue con el estado anterior hasta reiniciar la task."
                % (esc(titulo), dur, nums))
    finally:
        c.close()
        if snap_sel:
            _restore_model(sid, snap_sel)


def running_turns(max_age=7200):
    """Sesiones con un turno corriendo ahora, según el log del CLI
    (replay started/completed/failed sobre los últimos 2 logs)."""
    lim = time.time() - max_age
    out = {}
    for ts, sid, ev, _d in zc_logs.iter_events(
            BASE, needles=("turn.",), since_epoch=lim,
            last_files=2, tail_bytes=0):
        if ev == "turn.started":
            out[sid] = ts
        elif ev in ("turn.completed", "turn.failed"):
            out.pop(sid, None)
    return out



# ---------------------------------------------------------------- U1: bot unificado

MODEL_SEL = {"providerId": "zai-bot", "modelId": "GLM-5.3",
             "options": {"reasoningLevel": "high"}}
# Niveles por modelo (REFERENCES.md #8/#9): GLM-5.3 = low/high/max sin off;
# GLM-5.2 admite un set más amplio. Default conservador para modelos nuevos.
THINKING_LEVELS = ("max", "high", "low")
MODEL_THINKING = {
    "GLM-5.3": ("max", "high", "low"),
    "GLM-5.3-Flash": ("max", "high", "low"),
}


def _thinking_levels(model_id):
    return MODEL_THINKING.get(model_id, THINKING_LEVELS)
MODELOS = ("GLM-5.3", "GLM-5.3-Flash")


def _cur_sel(act):
    """Model selection vigente de la conversación activa (dict sane con
    provider/modelId/options.reasoningLevel)."""
    sel = dict(MODEL_SEL)
    sel["options"] = dict(MODEL_SEL.get("options") or {})
    if isinstance(act.get("model"), dict):
        sel.update({k: v for k, v in act["model"].items() if k != "options"})
        sel["options"] = dict(act["model"].get("options") or sel["options"])
    return sel


def _set_sel(act, model_id=None, thinking=None):
    """Cambia modelo o thinking de la activa PRESERVANDO lo otro."""
    sel = _cur_sel(act)
    if model_id:
        sel["modelId"] = model_id
    if thinking:
        sel["options"] = dict(sel.get("options") or {}, reasoningLevel=thinking)
    set_active_bot_session(dict(act, model=sel))


def _bot_session_state_path():
    return os.path.join(BASE, "hooks", "state", "bot-session.json")


def active_bot_session():
    return _store("bot-session.json").read() or {}


def set_active_bot_session(d):
    try:
        _store("bot-session.json").write(d)
    except OSError as e:
        log("errors", "set_active_bot_session: %s" % e)


def _hook_sid(payload):
    sid = first(payload, "session_id", "sessionId")
    return str(sid) if sid else None


def _task_current_ok(cfg, sid):
    """F15 — filtro anti-ruido: una sesión puede hacer push al chat solo si
    es la task current (bot-session.json leído EN VIVO en cada send: un
    /task switch corta el stream a mitad de turno). Las respuestas directas
    a comandos/mensajes del usuario NO pasan por acá. Sin sid activo no hay
    push de task (obliga a elegir con /task)."""
    if not cfg.get("only_active_task", True):
        return True
    if not sid:
        return False
    return sid == active_bot_session().get("sid")


def _worker_owned(sid):
    """F15 — sesión con worker del bot corriendo: el worker YA reporta su
    turno (caja viva, CoT, ✅/⏹️). Los hooks ▶️/✅/🔐/❓/🟢 de esa sesión
    solo duplicarían (doble-stream verificado: payloads de turno headless
    con el HEADLESS_GUARD disparando UserPromptSubmit+Stop además de la
    caja). Además un hook con wait_reply competiría por el offset."""
    return bool(sid) and WORKERS.get(sid) is not None


DEFAULT_WS = os.path.join(BASE, "workspace", "default")  # tasks sueltas (grupo «Tasks»)


def _tasks_scope(scope, limit):
    """/task → solo tasks con Project (excluye las sueltas del workspace
    default); /all_tasks → todas bajo el grupo Tasks."""
    tasks = ctx_tasks(limit)
    if scope != "all":
        tasks = [t for t in tasks if t["ws"] != DEFAULT_WS]
    # renumerar según la lista QUE SE MUESTRA: el n tipeado (/task n) tiene
    # que calzar con el número visible, no con la posición en la lista completa
    for i, t in enumerate(tasks, 1):
        t["n"] = i
    return tasks


def task_list_report(scope="projects"):
    """/task → lista tocable (Template Method: zcode_bot.reports)."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    activos = running_turns()
    act = active_bot_session()

    def marca_de(t):
        return ("🔄" if t["sid"] in activos
                else ("🎯" if act and t["sid"] == act.get("sid") else "·"))

    def line_of(t, i):
        tag = "" if t["ws"] == active_workspace_path() \
            else " · 📁" + esc(os.path.basename(t["ws"] or ""))
        return "%s %d. %s%s" % (marca_de(t), t["n"],
                                esc(t["title"] or t["sid"][-8:]), tag)

    def button_of(t, i):
        return {"text": "%s %d · %s" % (marca_de(t), t["n"],
                                        (t["title"] or t["sid"][-8:])[:34]),
                "callback_data": "t:%s" % t["sid"]}

    header = ("💬 <b>Todas las conversaciones</b> (incluye sueltas) — /task solo proyectos"
              if scope == "all"
              else "💬 <b>Conversaciones por proyecto</b> — tocá una (o /task n)")
    return zc_reports.build_list_report(
        tasks, header,
        "🧠 Sin tasks%s — /all_tasks muestra todas."
        % ("" if scope == "all" else " de proyecto"),
        line_of, button_of,
        "Texto libre que mandes va a la conversación activa (🎯).")


def task_select(n, scope="projects"):
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    if n < 1 or n > len(tasks):
        return "⚠️ Número fuera de la lista (1 a %d) — mandá /task." % len(tasks)
    t = tasks[n - 1]
    return _task_select_sid(t["sid"], t["title"])


def _task_select_sid(sid, titulo=None):
    """Fija la conversación activa por sessionId (los botones la usan: la
    posición numérica puede repetirse entre workspaces, el sid no)."""
    tasks = {t["sid"]: t for t in ctx_tasks(30)}
    t = tasks.get(sid)
    if not t:
        return "⚠️ No encontré esa conversación — mandá /task."
    prev = active_bot_session() or {}
    nueva = {"sid": t["sid"], "title": t["title"] or t["sid"][-8:],
             "ws": t["ws"]}
    # F22: la config del dueño SOBREVIVE el switch (antes /model, /thinking
    # y /mode se perdían al cambiar de conversación)
    for k in ("model", "mode"):
        if prev.get(k):
            nueva[k] = prev[k]
    set_active_bot_session(nueva)
    activos = running_turns()
    extra = " (⚠️ está corriendo un turno ahora)" if t["sid"] in activos else ""
    reply = ("🎯 Conversación activa: «%s»%s\nTodo texto que mandes va ahí "
             "(corre headless — sirve con la pantalla bloqueada)."
             % (esc(t["title"] or t["sid"][-8:]), extra))
    resume = _queue_resume(load_config(), t["sid"], t["title"])  # F15: cola dormida
    if resume:
        reply += "\n" + resume
    return reply


def _proto_ready(c, sid, model_sel=None, mode=None):
    """resume + setModel + setMode. El bot SIEMPRE fuerza un modo explícito:
    sin modo guardado → yolo (decisión de diseño: los prompts remotos corren
    sin diálogos de permiso; una sesión en build/edit headless se queda sin
    nadie que apruebe y el agente termina pidiendo "reiniciá ZCode").
    Devuelve None o msg de error."""
    r = c.request("session/resume", {"sessionId": sid}, timeout=60)
    if "error" in r:
        return "❌ resume: " + esc(json.dumps(r["error"])[:150])
    r = c.request("session/setModel",
                  {"sessionId": sid, "model": model_sel or MODEL_SEL}, timeout=30)
    if "error" in r:
        return "❌ setModel: " + esc(json.dumps(r["error"])[:150])
    r = c.request("session/setMode",
                  {"sessionId": sid, "mode": mode or "yolo"}, timeout=30)
    if "error" in r:
        return "❌ setMode: " + esc(json.dumps(r["error"])[:150])
    return None


def _wait_cli_event(sid, t0, eventos, timeout=200):
    """Espera en el log del CLI un evento de `sid` con ts >= t0. Devuelve el
    dict del evento (el ÚLTIMO que matchee) o None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fin = None
        for _ts, _s, _ev, d in zc_logs.iter_events(
                BASE, needles=tuple(eventos), sids={sid}, since_epoch=t0,
                last_files=1, tail_bytes=0):
            fin = d
        if fin is not None:
            return fin
        time.sleep(3)
    return None


def _model_snapshot(sid):
    """Última runtime/model_selection de la sesión (data JSON cruda) o None
    (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.model_selection(BASE, sid)


def _restore_model(sid, snap):
    """Devuelve la selección a su estado previo (o la borra si no había):
    el setModel zai-bot del bot NO debe quedar pegado en sesiones de la app
    (su registry no conoce zai-bot y se traba el picker)."""
    import sqlite3
    try:
        db = sqlite3.connect(os.path.join(BASE, "cli", "db", "db.sqlite"))
        if snap is None:
            db.execute("DELETE FROM session_entry WHERE session_id=? "
                       "AND type='runtime/model_selection'", (sid,))
        else:
            db.execute("UPDATE session_entry SET data=? WHERE session_id=? "
                       "AND type='runtime/model_selection'", (snap, sid))
        db.commit()
        db.close()
    except sqlite3.Error as e:
        log("errors", "_restore_model(%s): %s — el model selection puede "
            "haber quedado en zai-bot" % (sid, e))


def _resolve_ws(tag):
    """Resuelve un tag de proyecto ('bot', 'cv', haskell...) a path."""
    act = active_workspace_path()
    if not tag:
        return act
    tagl = tag.strip().strip("/").lower()
    cands = set([act]) | {t["ws"] for t in ctx_tasks(20) if t.get("ws")}
    for w in cands:
        if w and os.path.basename(w).lower() == tagl:
            return w
    for w in cands:
        if w and tagl in os.path.basename(w).lower():
            return w
    return None


def _create_session(c, ws):
    """session/create; devuelve (sid, None) o (None, msg_error). La sesión
    vive mientras viva el cliente: crearla dentro del worker del turno."""
    r = c.request("session/create",
                  {"workspace": {"workspaceKey": ws, "workspacePath": ws}},
                  timeout=60)
    if "error" in r:
        return None, "❌ create: " + esc(json.dumps(r["error"])[:150])
    ses = ((r.get("result") or {}).get("session")) or {}
    sid = ses.get("sessionId") or ses.get("id")
    if not sid:
        return None, "❌ create: sin sessionId en la respuesta"
    return sid, None


def bot_stop(sid, titulo=None):
    """Frena una corrida iniciada DESDE el celular Y limpia su cola
    (pedido F17: /stop también descarta los mensajes encolados de esa
    task). F16: el frenado va por marker — el WORKER lo ve en su loop y
    ejecuta session/stop con su propio runtime (stop real)."""
    n = len(_queue_take(sid))
    tit = WORKERS.flag_stop(sid)
    if tit is None:
        if n:
            return ("🧹 Limpié la cola de «%s» (%d mensajes) — no había "
                    "corrida del bot que frenar."
                    % (esc(titulo or sid[-8:]), n))
        return ("🤔 Esa corrida ya no existe (terminó o no fue iniciada "
                "desde el celular).")
    _stop_request(sid)
    extra = (" · 🧹 cola limpiada (%d)" % n) if n else ""
    return "🛑 Frenando «%s»…%s" % (esc(tit or titulo or sid[-8:]), extra)


def stop_list_report():
    """/stop → corridas del celular + colas dormidas (Template Method)."""
    items = WORKERS.items()
    colas = _queue_sids_with_items()

    def mins_de(par):
        return max(0, int((time.time() - par[1].get("t0", time.time())) / 60))

    def line_of(par, i):
        sid, w = par
        n = len(_queue_peek(sid))
        cola = ("\n    ⏳ %d encolado%s (se limpia al frenar)" % (n, "s" if n != 1 else "")) if n else ""
        return "%d. «%s» — %d min\n    %s%s" % (
            i, esc(w.get("titulo") or sid[-8:]), mins_de(par),
            esc(preview(w.get("prompt") or "", 70)), cola)

    def button_of(par, i):
        sid, w = par
        return {"text": "⏹ %d · %s (%dm)" % (i,
                                              (w.get("titulo") or sid[-8:])[:30],
                                              mins_de(par)),
                "callback_data": "s:%s" % sid}

    # sesiones con cola pero SIN worker: la corrida es de la Mac (no se
    # frena desde acá) — ofrecer igual la limpieza de su cola
    extras = [(sid, None) for sid in colas
              if WORKERS.get(sid) is None]

    def line_cola(par, i):
        sid, _ = par
        act = active_bot_session()
        marca = " 🎯" if act and sid == act.get("sid") else ""
        n = len(_queue_peek(sid))
        return "%d. ⏳ Cola de «%s»%s — %d mensaje%s (corrida de la Mac)" % (
            i, esc(sid[-8:]), marca, n, "s" if n != 1 else "")

    def button_cola(par, i):
        return {"text": "🧹 %d · limpiar cola" % i,
                "callback_data": "s:%s" % par[0]}

    cuerpo = zc_reports.build_list_report(
        items, "🛑 <b>Corridas del celular — tocá para frenar</b>",
        "😌 No hay nada corriendo iniciado desde el celular.\n"
        "Lo que corre en la Mac se frena con el botón Stop de la app.",
        line_of, button_of) if items else None
    cola_report = zc_reports.build_list_report(
        extras, "📬 <b>Mensajes en cola (Mac ocupada) — tocá para limpiar</b>",
        None, line_cola, button_cola) if extras else None
    if cuerpo and cola_report:
        return cuerpo + "\n\n" + cola_report
    return cuerpo or cola_report or zc_reports.build_list_report(
        [], "🛑 <b>Corridas del celular — tocá para frenar</b>",
        "😌 No hay nada corriendo iniciado desde el celular.\n"
        "Lo que corre en la Mac se frena con el botón Stop de la app.",
        line_of, button_of)


# ---------------------------------------------------------------- nuclear

LAUNCH_LABEL = "com.zcode.telegram-cmds"


def _nuc_pids(pgrep_args, run=None):
    """PIDs según pgrep con los args dados (inyectable para tests)."""
    run = run or subprocess.run
    r = run(["pgrep"] + list(pgrep_args), capture_output=True, text=True)
    return [int(x) for x in (r.stdout or "").split()]


def _nuc_wait(fn_bool, timeout, sleep=None, now=None):
    """Espera hasta que fn_bool() dé True o venza el timeout (0.5s por paso).
    now inyectable para tests (reloj falso, sin esperas reales)."""
    sleep = sleep or time.sleep
    now = now or time.time
    deadline = now() + timeout
    while now() < deadline:
        if fn_bool():
            return True
        sleep(0.5)
    return fn_bool()


def _nuc_kill_own_runtimes(run=None):
    """F16: SIGTERM a los WORKERS del registry (procesos independientes del
    poller — los runtimes ya no son hijos de acá, son nietos). Grace de 5s
    y SIGKILL al sobreviviente. Los workers también recibieron el marker
    de /stop (bot_stop arriba): esto es el remate duro."""
    run = run or subprocess.run
    import signal as _signal
    pids = []
    for _sid, w in WORKERS.items():
        pid = w.get("pid")
        if pid and pid != os.getpid():
            pids.append(int(pid))
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass
    if pids:
        time.sleep(5)

        def vivo(p):
            try:
                os.kill(p, 0)
                return True
            except OSError:
                return False
        for pid in pids:
            if vivo(pid):
                try:
                    os.kill(pid, _signal.SIGKILL)
                except OSError:
                    pass


def app_restart_cycle(run=None, pids=None, sleep=None, now=None):
    """Ciclo completo de reinicio de la app ZCode (para /nuclear):
    SIGTERM (cierre normal, sin permisos de osascript) → espera de salida
    (SIGKILL de rescate si no cierra) → host huérfano solo si la app YA
    murió (única excepción a la regla, ADR-006) → cli huérfanos (ppid 1)
    rematados → recién con el árbol COMPLETO muerto se reabre con open -a.
    Sin sobrevivientes no quedan registraciones que el Dock muestre como
    íconos "exec". Devuelve {had_app, app_ok, notes}. Inyectable p/ tests."""
    run = run or subprocess.run
    pids = pids or _nuc_pids
    sleep = sleep or time.sleep
    now = now or time.time

    def app_up():
        return bool(pids(["-x", "ZCode"]))

    def host_up():
        return bool(pids(["-f", "zcode-host-local"]))

    def orphans():
        return pids(["-P", "1", "zcode-cli"])  # cli reparentados a launchd

    rep = {"had_app": app_up(), "app_ok": False, "notes": []}
    if rep["had_app"]:
        run(["killall", "-TERM", "ZCode"], capture_output=True)
        if not _nuc_wait(lambda: not app_up(), 20, sleep, now):
            run(["killall", "-KILL", "ZCode"], capture_output=True)
            rep["notes"].append("la app no cerró con SIGTERM (SIGKILL)")
            _nuc_wait(lambda: not app_up(), 5, sleep, now)
    if host_up():  # muere con la app; si queda colgado:
        if not _nuc_wait(lambda: not host_up(), 15, sleep, now):
            run(["pkill", "-TERM", "-f", "zcode-host-local"], capture_output=True)
            rep["notes"].append("host huérfano terminado a mano")
            _nuc_wait(lambda: not host_up(), 5, sleep, now)
    vivos = orphans()
    for pid in vivos:
        run(["kill", "-TERM", str(pid)], capture_output=True)
    if vivos and not _nuc_wait(lambda: not orphans(), 5, sleep, now):
        for pid in vivos:
            run(["kill", "-KILL", str(pid)], capture_output=True)
        rep["notes"].append("cli huérfanos rematados con SIGKILL")
        _nuc_wait(lambda: not orphans(), 3, sleep, now)
    # árbol completo muerto ANTES de reabrir: estado limpio para el Dock
    _nuc_wait(lambda: not (app_up() or host_up() or orphans()), 5, sleep, now)
    run(["open", "-a", "ZCode"], capture_output=True)
    rep["app_ok"] = _nuc_wait(app_up, 40, sleep, now)
    return rep


def _nuclear_flag_path():
    return os.path.join(STATE_DIR, "bot-nuclear")


_NUCLEAR_TTL = 120  # sin heartbeat hace 120s = huérfano


def _nuclear_heartbeat():
    """Nuclear 2.0: refresca el flag en CADA fase del ciclo. El TTL ya no
    es 'edad del archivo' sino 'tiempo sin heartbeat' — un nuclear real de
    cualquier duración no se purga a mitad (con app rebelde puede tardar
    ~95s+). El huérfano del incidente 18:56 sigue auto-sanando igual:
    cuando el proceso muere, nadie hace touch."""
    p = _nuclear_flag_path()
    try:
        with open(p, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _nuclear_active():
    """F16: en ARCHIVO (el flag in-memory moriría con el poller que el
    propio nuclear kickstea al final). F21: TTL contra huérfanos (incidente
    18:56: bloqueó TODOS los deploys en silencio hasta 22:33). Nuclear 2.0:
    el ciclo refresca el flag por heartbeat, así que el TTL mide inactividad."""
    p = _nuclear_flag_path()
    try:
        if time.time() - os.path.getmtime(p) > _NUCLEAR_TTL:
            os.remove(p)  # auto-sanación: el gate queda desbloqueado
            pwlog("bot-nuclear huérfano purgado (TTL)")
            return False
        return True
    except OSError:
        return False


def _nuclear_run(cfg):
    """☢️ Nuclear 2.0: operación anunciada y verificada. Corre en un
    thread del poller. El flag lo escribe acá (heartbeat en cada fase);
    el finally es solo rescate — la limpieza NORMAL la hace el watchdog
    post-reinicio, que sobrevive al kickstart."""
    _nuclear_heartbeat()
    try:
        _nuclear_body(cfg)
    finally:
        # rescate: si el body murió ANTES de spawnear el watchdog, el flag
        # no puede quedar bloqueando deploys (TTL lo purgaría, pero esto
        # es inmediato). Si el watchdog ya corre, este remove es inofensivo:
        # el watchdog guarda su propia copia del reporte.
        try:
            os.remove(_nuclear_flag_path())
        except OSError:
            pass


def _nuclear_snapshot():
    """Pre-vuelo: qué hay vivo ahora mismo (para anunciar lo que se pierde)."""
    workers = list(WORKERS.items())
    mac = {sid: ts for sid, ts in running_turns().items()
           if WORKERS.get(sid) is None}
    cola = sum(len(_queue_peek(sid)) for sid in _queue_sids_with_items())
    return workers, mac, cola


def _nuclear_report_path():
    return os.path.join(STATE_DIR, "bot-nuclear-report.json")


def _nuclear_body(cfg):
    t0 = time.time()
    pwlog("NUCLEAR2 inicio")
    # Fase 0 — pre-vuelo: anuncio honesto de lo que va a pasar
    _nuclear_heartbeat()
    workers, mac, cola = _nuclear_snapshot()
    send(cfg, "☢️ <b>Nuclear</b> — pre-vuelo:\n"
              "⏹️ %d corrida%s del celular van a frenarse\n"
              "🖥️ %d turno%s de la Mac en vuelo (se pierden con la app)\n"
              "📬 %d mensaje%s en cola (SE CONSERVAN — corren al volver)"
         % (len(workers), "s" if len(workers) != 1 else "",
            len(mac), "s" if len(mac) != 1 else "",
            cola, "s" if cola != 1 else ""), loud=True)
    # Fase 1+2 — cerradura activa (los guards leen _nuclear_active) + frenado
    _nuclear_heartbeat()
    send(cfg, "☢️ Frenando corridas del bot…", loud=False)
    frenadas = 0
    for sid, w in workers:
        tit = WORKERS.flag_stop(sid)  # SIN _queue_take: la cola se conserva
        if tit is not None:
            _stop_request(sid)
            frenadas += 1
            pwlog("NUCLEAR2 stop", sid[-8:], (tit or "")[:40])
    _nuc_kill_own_runtimes()
    # verificación: el registry tiene que quedar vacío
    vivos = len(WORKERS.items())
    _nuclear_heartbeat()
    # Fase 3 — app (los turnos de la UI mueren con ella, ADR-006)
    send(cfg, "☢️ Cerrando la app ZCode de la Mac (los %d turnos de la UI "
              "se pierden ahí)…" % len(mac), loud=False)
    rep = app_restart_cycle()
    _nuclear_heartbeat()
    # Fase 4 — watchdog post-reinicio: sobrevive al kickstart y cierra la
    # operación con veredicto (hoy el fracaso del arranque era silencioso)
    dur = int(time.time() - t0)
    reporte = {"old_pid": os.getpid(), "t0": t0, "dur": dur,
               "frenadas": frenadas, "totales": len(workers),
               "mac": len(mac), "cola": cola, "vivos": vivos,
               "app_ok": bool(rep.get("app_ok")),
               "notes": rep.get("notes", [])}
    try:
        _store("bot-nuclear-report.json").write(reporte)
    except OSError:
        pass
    _spawn_nuclear_watchdog()
    # kickstart: mata ESTE poller — nada de este hilo corre después
    pwlog("NUCLEAR2 kickstart (frenadas %d/%d, vivos %d)"
          % (frenadas, len(workers), vivos))
    time.sleep(2)
    try:
        subprocess.run(["launchctl", "kickstart", "-k",
                        "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)],
                       capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log("errors", "nuclear kickstart: %s" % exc)


def _spawn_nuclear_watchdog():
    """Proceso detached que verifica el reinicio y manda el reporte final.
    Debe sobrevivir al kickstart que mata a su padre (patrón F16)."""
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        err = open(os.path.join(LOG_DIR, "worker-stderr.log"), "a")
        subprocess.Popen([sys.executable, me, "--nuclear-watchdog"],
                         stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                         start_new_session=True, cwd=LOG_DIR)
        pwlog("NUCLEAR2 watchdog spawneado")
    except Exception as exc:  # noqa: BLE001 — sin watchdog, el TTL rescata
        log("errors", "nuclear watchdog spawn: %s" % exc)


def cmd_nuclear_watchdog():
    """☢️ Nuclear 2.0 — cierre verificado. Corre DETACHED (sobrevive al
    kickstart que mata al poller). Espera hasta 90s al poller nuevo:
    ✅ apareció → reporte final único; ⛔️ no apareció → aviso de fracaso.
    En AMBOS casos limpia el flag (nunca queda bloqueando deploys)."""
    cfg = load_config()
    rep = _store("bot-nuclear-report.json").read() or {}
    old_pid = rep.get("old_pid") or 0

    def poller_vivo():
        linea = subprocess.run(["pgrep", "-f",
                                "zcode-telegram.py watch-commands"],
                               capture_output=True, text=True).stdout
        for pid_txt in (linea or "").split():
            try:
                if int(pid_txt) != old_pid:
                    return int(pid_txt)
            except ValueError:
                continue
        return None

    nuevo = None
    deadline = time.time() + 90
    while time.time() < deadline and nuevo is None:
        nuevo = poller_vivo()
        if nuevo is None:
            time.sleep(2)
    dur = int(time.time() - (rep.get("t0") or time.time()))
    if nuevo is not None:
        time.sleep(3)  # que termine de arrancar antes del veredicto
        notes = (" · " + "; ".join(rep.get("notes", []))
                 if rep.get("notes") else "")
        send(cfg, ("☢️ <b>Nuclear completo y verificado</b> (%ds)\n"
                   "⏹️ %s/%s corridas del celular frenadas\n"
                   "🖥️ App: %s · 🖥️ %d turno%s de la Mac perdidos\n"
                   "📬 Cola conservada: %d mensaje%s (corren al volver)\n"
                   "🔁 Poller nuevo: pid %d ✓%s"
                   % (dur, rep.get("frenadas", "?"), rep.get("totales", "?"),
                      "arriba ✓" if rep.get("app_ok") else "NO volvió ⚠️",
                      rep.get("mac", 0), "s" if rep.get("mac") != 1 else "",
                      rep.get("cola", 0),
                      "s" if rep.get("cola") != 1 else "",
                      nuevo, esc(notes))),
             loud=True)
        pwlog("NUCLEAR2 verificado: poller nuevo", nuevo)
    else:
        send(cfg, "⛔️ <b>El poller NO volvió tras el nuclear</b> (90 s) — "
                  "revisá launchctl o abrí la app. Cuando vuelva: /ping. "
                  "La cola y los markers quedaron intactos.", loud=True)
        pwlog("NUCLEAR2 FALLÓ: poller no volvió")
    # limpieza SIEMPRE: el flag jamás bloquea deploys después de acá
    for p in (_nuclear_flag_path(), _nuclear_report_path()):
        try:
            os.remove(p)
        except OSError:
            pass


def _cmd_deploy(cfg, rest):
    """/deploy — el pipeline completo desde el celular (F25): tests →
    backup → copia → recarga instantánea. Confirmación en dos pasos
    (el 'go' solo lo produce el botón). Solo tiene sentido si HAY cambios
    que deployar: corre deploy.sh tal cual."""
    r = (rest or "").strip().lower()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    deploy_sh = os.path.join(os.path.dirname(src), "deploy.sh")
    if not os.path.exists(deploy_sh):
        # en producción (~/.zcode/hooks) el repo vive en el workspace del bot
        for cand in ("/Users/lattenero/Projects/bot/deploy.sh",):
            if os.path.exists(cand):
                deploy_sh = cand
                break
    if r == "go":
        def run():
            try:
                p = subprocess.run(["bash", deploy_sh], capture_output=True,
                                   text=True, timeout=600, cwd=os.path.dirname(deploy_sh))
                tail = (p.stdout or "").strip().splitlines()[-6:]
                ok = p.returncode == 0
                send(cfg, ("✅ <b>Deploy OK</b>\n<pre>%s</pre>" % esc("\n".join(tail)[-1500:]))
                     if ok else
                     ("❌ <b>Deploy falló</b> (rc=%d)\n<pre>%s</pre>"
                      % (p.returncode, esc("\n".join(tail)[-1200:]))),
                     loud=not ok)
            except Exception as exc:
                send(cfg, "❌ deploy: %s" % esc(str(exc)[:150]), loud=True)
        import threading as _th
        _th.Thread(target=run, daemon=True).start()
        return "🚀 Deploy corriendo (tests incluidos, ~1 min) — te aviso."
    if r == "no":
        return "🙅 Cancelado."
    return ("🚀 <b>Deploy</b> — tests → backup → copia → recarga "
            "instantánea del poller. ¿Corro deploy.sh?",
            {"inline_keyboard": [[
                {"text": "🚀 Deploy", "callback_data": "dep:go"},
                {"text": "🙅 No", "callback_data": "dep:no"}]]})


def _cmd_nuclear(cfg, rest):
    """/nuclear — EMERGENCIA: frena TODO (corridas del celular + tasks de la
    UI de la Mac) y reinicia la app y el bot. No hay entrada externa al
    runtime de la app (ADR-006): cerrar y reabrir ES el mecanismo. Solo se
    ejecuta con 'go' — que solo produce el botón de confirmación."""
    r = (rest or "").strip().lower()
    if r == "go":
        import threading as _th
        _th.Thread(target=_nuclear_run, args=(cfg,), daemon=True).start()
        return ("☢️ Nuclear 2.0 arrancando — pre-vuelo te dice qué se frena, "
                "qué se pierde y qué se conserva. El veredicto final llega "
                "verificado (watchdog post-reinicio).")
    if r == "no":
        return "🙅 Cancelado — no se tocó nada."
    return ("☢️ <b>Nuclear</b> — emergencia:\n"
            "⏹️ frena TODAS las corridas (celular + Mac)\n"
            "🖥️ cierra y reabre la app ZCode\n"
            "🔁 reinicia el bot\n\n¿Confirmás?",
            {"inline_keyboard": [[
                {"text": "☢️ Confirmar", "callback_data": "nuc:go"},
                {"text": "🙅 Cancelar", "callback_data": "nuc:no"}]]})


def _extract_assistant_text(msgs, solo_ultima=False):
    """Texto de los mensajes assistant: SOLO parts con type=="text" (sin
    reasoning/tool noise). Estructura real: {info:{role,id}, parts:[{type,text}]}."""
    ass = [m for m in (msgs if isinstance(msgs, list) else [])
           if isinstance(m, dict) and (m.get("info") or {}).get("role") == "assistant"]
    if solo_ultima and ass:
        ass = ass[-1:]
    out = []
    for m in ass:
        for part in m.get("parts") or []:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                out.append(part["text"])
    return "\n\n".join(t for t in out if t.strip())


def _fetch_messages(c, sid, after_id=None, limit=40):
    r = c.request("session/messages",
                  {"sessionId": sid, "limit": limit, **({"afterMessageId": after_id} if after_id else {})},
                  timeout=60)
    if "error" in r:
        return None
    res = r.get("result")
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("messages", "items", "data"):
            if isinstance(res.get(k), list):
                return res[k]
    return []


def _log_watch_paths():
    """Paths a vigilar para saber que el CLI escribió eventos: los mismos
    archivos que escanea iter_events (últimos 2) + el dir (rotación)."""
    d = os.path.join(BASE, "cli", "log")
    out = [d]
    try:
        logs = sorted((f for f in os.listdir(d)
                       if f.startswith("zcode-") and f.endswith(".jsonl")),
                      reverse=True)
        out.extend(os.path.join(d, f) for f in logs[:2])
    except OSError:
        pass
    return out


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
    from zcode_bot import fswatch
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
                    BASE, needles=("turn.started", "turn.completed",
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


def _last_id_of(msgs):
    for m in reversed(msgs if isinstance(msgs, list) else []):
        if isinstance(m, dict):
            info = m.get("info") or {}
            mid = info.get("id") or info.get("messageId") or m.get("messageID")
            if mid:
                return mid
    return None


# ---------------------------------------------------------------- trace

COT_CHUNK = 3000   # por mensaje: esc() puede inflar el texto, margen s/ 4096
COT_TAIL = 3200    # cola que se mantiene editable en la caja viva
EVENTS_TRIP = 1200  # replay mayor a esto → live OFF (anti-OOM, ver _live_view)
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


def _live_skip_path():
    return os.path.join(STATE_DIR, "bot-live-skip.json")


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
        os.makedirs(STATE_DIR, exist_ok=True)
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


def _steps_of_messages(msgs):
    """[(nombre, desc)] de los tool calls de una lista de mensajes (parts
    type=='tool': state.input trae command/file_path). Tolerante al shape."""
    return [(n, _tool_desc_corto(n, inp))
            for n, inp in _tools_of_messages(msgs)]


def _tools_of_messages(msgs):
    """F22bis — [(nombre, input_dict)] COMPLETO de los tool calls (el
    input es lo que alimenta las cajas permanentes: comando entero,
    old/new de edits, content de writes). Tolerante al shape."""
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        if not isinstance(m, dict):
            continue
        for p in m.get("parts") or []:
            if not (isinstance(p, dict) and p.get("type") == "tool"):
                continue
            st = p.get("state") or {}
            nombre = str(p.get("tool") or p.get("name") or st.get("name")
                         or "tool")
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            out.append((nombre, inp))
    return out


def _tool_desc_corto(nombre, inp):
    """Desc de una línea (índice de pasos, caja vija, trace)."""
    if isinstance(inp.get("command"), list):
        return " ".join(str(x) for x in inp["command"])[:90]
    if isinstance(inp.get("command"), str):
        return inp["command"][:90]
    for k in ("file_path", "path"):
        if inp.get(k):
            return str(inp[k])
    if inp.get("pattern"):
        return "pattern " + str(inp["pattern"])[:60]
    return ""


def _tool_caja(nombre, inp):
    """F22bis — texto de la caja PERMANENTE de un tool call: comando
    COMPLETO (pedido de Damian), diff real para edits/writes. Cap 3400
    chars (Telegram 4096 - formato). Devuelve HTML."""
    import difflib
    LOW = 3400
    desc = _tool_desc_corto(nombre, inp)
    head = "🔧 <b>%s</b>%s" % (esc(nombre),
                               (" — <code>%s</code>" % esc(preview(desc, 120)))
                               if desc else "")
    cuerpo = ""
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    if isinstance(cmd, str) and cmd.strip():
        cuerpo = "<pre>%s</pre>" % esc(cmd[:LOW])
        sal = inp.get("output") or inp.get("result") or ""
        if isinstance(sal, str) and sal.strip():
            cuerpo += "\n└ <pre>%s</pre>" % esc(sal[:800])
    elif nombre in ("Edit", "MultiEdit", "Replace") and inp.get("old_string") is not None:
        viejo = str(inp.get("old_string") or "").splitlines()
        nuevo = str(inp.get("new_string") or "").splitlines()
        d = list(difflib.unified_diff(viejo, nuevo, lineterm=""))[2:] or ["(sin cambios)"]
        cuerpo = "<pre>%s</pre>" % esc("\n".join(d)[:LOW])
    elif nombre in ("Write", "Create", "New") and inp.get("content"):
        lineas = str(inp["content"]).splitlines()
        cuerpo = ("<pre>%s</pre>\n<i>(+%d líneas)</i>"
                  % (esc("\n".join(lineas[:40])[:LOW]), len(lineas)))
    elif nombre in ("Read", "View"):
        cuerpo = "<i>leyendo</i> <code>%s</code>" % esc(preview(desc, 160))
    else:
        try:
            cuerpo = "<pre>%s</pre>" % esc(json.dumps(inp, ensure_ascii=False)[:LOW])
        except (TypeError, ValueError):
            cuerpo = ""
    return head + ("\n" + cuerpo if cuerpo else "")


def _cajas_paso_nuevas(cfg, titulo, contador, nuevos):
    """F22bis — manda como mensajes PERMANENTES (scrolleables como las de
    CoT) cada tool call nuevo: comando completo + diff. Config
    tools_stream=false apaga."""
    if not cfg.get("tools_stream", True):
        return
    for nombre, inp in nuevos:
        contador[0] += 1
        try:
            send(cfg, "%s · <b>%d</b>" % (_tool_caja(nombre, inp),
                                          contador[0]), loud=False)
            if contador[0] % 3 == 0:
                time.sleep(1.0)  # pace ~1 msg/s del chat
        except Exception:  # noqa: BLE001
            pass


def _reasoning_of_messages(msgs):
    """Razonamiento completo y EN ORDEN de los mensajes assistant
    (parts type=='reasoning' — el CoT queda persistido en el historial)."""
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        if not (isinstance(m, dict) and (m.get("info") or {}).get("role") == "assistant"):
            continue
        for p in m.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "reasoning":
                t = p.get("text") or p.get("reasoning") or ""
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
    return "\n\n".join(out)


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
            "%s/bot%s/sendDocument" % (API, cfg.get("token") or ""),
            data=body, headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=90) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as exc:  # noqa: BLE001 - nunca romper el turno
        log("errors", "sendDocument: %s" % exc)
        return False


def build_trace(titulo, sid, dur_min, msgs, live_cot=""):
    """Documento .txt del turno: pasos numerados + razonamiento completo +
    respuesta. Autoritativo: el historial (msgs); live_cot es fallback si
    el historial no trajera parts de reasoning."""
    import datetime
    steps = _steps_of_messages(msgs)
    cot = _reasoning_of_messages(msgs) or live_cot
    answer = _extract_assistant_text(msgs)
    L = ["╔═ TURNO: %s" % (titulo or "?"),
         "║ sesión: %s" % (sid or "?"),
         "║ %s · %.1f min" % (datetime.datetime.now().strftime(
             "%Y-%m-%d %H:%M"), dur_min),
         "╚" + "═" * 26, ""]
    L.append("════════ PASOS (%d) ════════" % len(steps))
    for i, (nombre, desc) in enumerate(steps, 1):
        L.append("%3d. %s%s" % (i, nombre, (" · " + desc) if desc else ""))
    L.append("")
    L.append("════════ RAZONAMIENTO COMPLETO (%d chars) ════════" % len(cot))
    L.append(cot or "(sin razonamiento registrado)")
    L.append("")
    L.append("════════ RESPUESTA ════════")
    L.append(answer or "(sin texto de respuesta)")
    return "\n".join(L)


def _turn_trace(cfg, titulo, sid, dur_min, msgs, live_cot=""):
    """Al terminar un turno: persiste el trace y LO MANDA SOLO si
    trace_documents=true (default OFF desde F19 — pedido de Damian: el
    CoT ya queda scrolleable en las cajas del chat; el archivo era ruido
    redundante. /cot lo genera on-demand cuando se pide). El archivo
    state/last-turn-trace.txt queda siempre."""
    try:
        trace = build_trace(titulo, sid, dur_min, msgs, live_cot=live_cot)
    except Exception as exc:  # noqa: BLE001 - nunca romper la entrega
        log("errors", "build_trace: %s" % exc)
        return
    try:
        with open(os.path.join(STATE_DIR, "last-turn-trace.txt"), "w") as f:
            f.write(trace)
    except OSError:
        pass
    if not cfg.get("trace_documents", False):
        return
    import datetime
    fname = "turn-%s-%s.txt" % (
        re.sub(r"[^A-Za-z0-9._-]+", "-", (titulo or "ses")[:24]).strip("-"),
        datetime.datetime.now().strftime("%H%M"))
    np = len(_steps_of_messages(msgs))
    ncot = len(_reasoning_of_messages(msgs) or live_cot)
    send_document(cfg, fname, trace,
                  "🧠 Trace «%s»: %d pasos · CoT %s chars · respuesta aparte"
                  % (titulo or "?", np, fmt_tok(ncot)))


# F16: registry de workers en ARCHIVO (state/bot-workers.json, flock) —
# la misma vista para poller y workers-proceso; un restart del poller no
# borra los turnos en curso (era: threads daemon con estado en memoria).
WORKERS = zc_conc.WorkersRegistry(os.path.join(STATE_DIR, "bot-workers.json"))
# F16: preguntas ❓ por archivo (state/bot-q/) — la respuesta del chat
# cruza del poller al proceso worker sin memoria compartida
QUESTIONS = zc_conc.QuestionFiles(os.path.join(STATE_DIR, "bot-q"))


def pwlog(*a):
    """Log de pasos del bot unificado (worker + entradas del poller).
    F24: con FECHA (la reconstrucción forense del 13/10 exigió cruzar 3
    fuentes para fechar líneas) y rotación por tamaño."""
    import datetime
    try:
        p = os.path.join(BASE, "hooks", "logs", "prompt-worker.log")
        _log_rotate(p)
        with open(p, "a") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now()
                                   .strftime("%m-%d %H:%M:%S"),
                                   " ".join(str(x) for x in a)))
    except OSError:
        pass


def _log_rotate(path, max_bytes=5 * 1024 * 1024):
    """>5MB → .1 (una generación: el log es diagnóstico, no archivo)."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


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
            os.path.join(BASE, "hooks", "backups", "*")) if os.path.isdir(g))
        for p in bks[:-10]:
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            borrados += 1
    except OSError:
        pass
    borrados += viejos(os.path.join(LOG_DIR, "payload-*.json"), 2)
    borrados += viejos(os.path.join(LOG_DIR, "proto-stderr-*.log"), 1,
                       vacios=True)
    borrados += viejos(os.path.join(STATE_DIR, "bot-q", "*.json"), 0.1)
    borrados += viejos(os.path.join(STATE_DIR, "bot-stop", "*"), 0.5)
    borrados += viejos(os.path.join(STATE_DIR, "turn-*.marker"), 1)
    _log_rotate(os.path.join(LOG_DIR, "errors.log"))
    if borrados:
        pwlog("housekeeping: %d artefactos purgados" % borrados)


def _rss_mb():
    """RSS máximo del proceso en MB (ru_maxrss viene en bytes en macOS)."""
    import resource
    try:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576)
    except Exception:
        return -1


def _user_input_handler(cfg, sid, titulo):
    """Callback de interaction/requestUserInput: manda la ❓ al chat y
    espera la respuesta por ARCHIVO (F16: el poller la escribe al tocar
    el botón/número — no hay memoria compartida entre procesos). F15: la
    pregunta lleva SU sid — con varias tasks la respuesta enruta a la que
    preguntó (handle_free_text prioriza la task current)."""
    def handler(params):
        qs = params.get("questions") or []
        rid = str(params.get("requestId") or time.time())
        # /task new: el sid real nace después del spawn — para esa ventana
        # vale el que fija el worker como sesión activa
        QUESTIONS.ask(rid, qs, titulo,
                      sid or (active_bot_session().get("sid")))
        lines = ["❓ <b>%s</b>" % esc(titulo)]
        kb = []
        if len(qs) == 1:
            # una sola pregunta: los botones mapean 1:1 con las opciones que
            # usa la resolución por número (handle_free_text usa qs[0])
            for i, op in enumerate(qs[0].get("options") or [], 1):
                d = (" — " + esc(op["description"][:60])) if op.get("description") else ""
                lines.append("  %d. %s%s" % (i, esc(op.get("label", "?")), d))
                kb.append([{"text": ("%d. %s" % (i, (op.get("label") or "?")))[:36],
                            "callback_data": "ans:%d" % i}])
        else:
            for q in qs:
                if q.get("question"):
                    lines.append(esc(q["question"]))
                for i, op in enumerate(q.get("options") or [], 1):
                    d = (" — " + esc(op["description"][:60])) if op.get("description") else ""
                    lines.append("  %d. %s%s" % (i, esc(op.get("label", "?")), d))
        lines.append("\nRespondé con el número (o tocá un botón)." if kb
                     else "\nRespondé con el número (o texto libre).")
        send(cfg, "\n".join(lines), loud=True,
             markup={"inline_keyboard": kb} if kb else None)
        resp = QUESTIONS.wait(rid, timeout=480)
        if resp is not None:
            return {"action": "accept",
                    "content": {"answer": resp}}
        send(cfg, "⌛️ La pregunta de «%s» caducó sin respuesta." % esc(titulo))
        return {"action": "decline", "reason": "timeout telegram"}
    return handler


def _queue_put(sid, act, text):
    def add(q):
        q = q if isinstance(q, dict) else {}
        lst = q.get(sid) or []
        lst.append({"act": act, "text": text[:6000]})
        q[sid] = lst[-5:]
        return q
    try:
        # F16: flock — poller y workers-proceso escriben la misma cola
        _store("bot-queue.json").update_cross_process(add, {})
    except OSError:
        pass


def _queue_take(sid):
    taken = [[]]

    def take(q):
        q = q if isinstance(q, dict) else {}
        taken[0] = q.pop(sid, [])
        return q
    try:
        _store("bot-queue.json").update_cross_process(take, {})
    except OSError:
        pass
    return taken[0]


def _live_view(c, cfg, sid, t0, titulo, msg_id, th, last_id=None):
    """Sidecar de un turno headless. Tres mensajes vivos/permanentes:
    1) caja viva del razonamiento/texto (deltas de session/events, dedupe
       por eventId, sin acumular payloads — guardas de tamaño de la fuga).
    2) cajas PERMANENTES de CoT: al llenarse la cola (4096/mensaje es límite
       duro de Telegram) el excedente se manda como mensajes fijos «CoT N»
       — el razonamiento queda scrolleable de principio a fin MIENTRAS
       corre el turno (config cot_stream=false lo apaga).
    3) «pasos»: los tool calls completados del historial (afterMessageId)
       — 🔧 comandos, archivos, tools.
    Devuelve (coT_completo, texto, pasos) para el trace final (fallback si
    el historial no trajera parts). Corta cuando el turno termina o 35 min."""
    razon = ""      # solo la fase de pensado EN CURSO (se resetea al escribir)
    razon_n = 0     # chars totales pensados (para el log — el texto vive en cot_full)
    texto = ""      # COLA de lo escrito (últimos 8KB, para la caja viva)
    texto_n = 0     # chars totales escritos (el texto íntegro sale del historial)
    n_deltas = 0
    vistos = set()
    pasos = []
    pasos_nmsg = [0]    # F22bis: contador de cajas de tools (para el pace)
    ph_pasos = None
    pasos_last = last_id
    last_edit = 0.0
    last_poll_pasos = 0.0
    cot_full = ""    # CoT pendiente de mandar (lo enviado al chat se LIBERA)
    cot_flushed = 0  # chars ya enviados como mensajes permanentes
    cot_nmsg = 0
    live_id = msg_id  # caja viva: se MUDA abajo cuando hay cajas de CoT
    live_moved = False
    evento_write = False
    primer_poll = True  # instrumentación anti-OOM: tamaño del primer replay
    events_off = False  # anti-OOM: session/events devuelve el replay COMPLETO
    # de la sesión en cada llamada; en sesiones gigantes, parsearlo cada 2s
    # infla la RSS hasta que macOS mata el poller (2.9GB real, turno perdido).
    if _live_skip_check(sid):
        # flag de un turno anterior: el replay de ESTA sesión ya pesa GBs
        # (imágenes) — ni un solo parseo; pasos y documento final siguen
        events_off = True
        pwlog("live events OFF de arranque (flag replay gigante)")

    def cot_flush(tail_keep, pace=True):
        """Manda como mensajes PERMANENTES el CoT ya acumulado (cajas que
        quedan scrolleables arriba); devuelve nada. Pausado para respetar
        el ~1 mensaje/s por chat de la FAQ de Telegram. SIN tope de
        mensajes: se mandan todos (petición de Damian 2026-10-13)."""
        nonlocal cot_flushed, cot_nmsg, live_id, live_moved
        nonlocal cot_full  # sin esto, la asignación de liberación la vuelve
        # LOCAL y la primera lectura (línea _cot_chunks_to_send) explota con
        # UnboundLocalError — turno muerto (incidente 14:28 "Probando cot 1234")
        if not cfg.get("cot_stream", True):
            return
        trozos, nf = _cot_chunks_to_send(
            cot_full, cot_flushed, tail_keep=tail_keep)
        for i, trozo in enumerate(trozos):
            if i and pace:
                time.sleep(1.2)
            cot_nmsg += 1
            send(cfg, "🧠 <b>«%s»</b> · CoT %d\n<pre>%s</pre>"
                 % (esc(titulo), cot_nmsg, esc(trozo)), loud=False)
        if nf:
            # MEMORIA: lo ya entregado al chat no se retiene — el prefijo
            # enviado se suelta y el documento final sale del historial
            cot_full = cot_full[nf:]
            cot_flushed = 0
        if trozos and not live_moved and live_id:
            # el "en vivo" se muda al final del chat: la caja viva queda
            # ABAJO de las cajas congeladas, donde se está leyendo
            live_moved = True
            live_id = _live_send(cfg, "🧠 <b>«%s»</b> — pensando…"
                                 % esc(titulo)) or live_id

    deadline = time.time() + 2100
    cad = 2.0  # cadencia del poll (adaptiva: frame grande → 5 s, ver abajo)
    segundo_plano = False  # F15: la task dejó de ser la current a mitad de turno
    stop_pedidos = 0       # F16: /stop por marker — session/stop UNA vez
    while time.time() < deadline:
        if not th.is_alive():
            break
        if stop_pedidos < 1 and _stop_requested(sid):
            # /stop: frenar con el runtime PROPIO del worker (stop real).
            # El turno termina enseguida y th corta este loop naturalmente.
            stop_pedidos += 1
            pwlog("stop marker: session/stop %s" % sid)
            try:
                c.request("session/stop", {"sessionId": sid}, timeout=30)
            except Exception:  # noqa: BLE001
                pass
        if not segundo_plano and not _task_current_ok(cfg, sid):
            # F15: silenciar el stream (CoT/cajas/pasos) con UN edit de
            # despedida; el turno sigue y la RESPUESTA FINAL igual llega
            # (prompt_worker) — es respuesta al mensaje del usuario, no push
            segundo_plano = True
            pwlog("live pausado: %s dejó de ser la task current" % sid)
            try:
                _live_edit(cfg, msg_id,
                           "⏸️ <b>«%s»</b> — siguió en segundo plano "
                           "(cambiaste de task); aviso al terminar"
                           % esc(titulo))
            except Exception:  # noqa: BLE001
                pass
        try:
            if events_off:
                # trip activado: no volver a pedir el replay gigante —
                # los pasos y el documento final cubren el resto del turno
                pass
            else:
                r = c.request("session/events", {"sessionId": sid}, timeout=20)
                evs = (r.get("result") or {}).get("events") or []
                if primer_poll:
                    primer_poll = False
                    cad = _poll_cadence(c.max_frame)
                    pwlog("live evs=%d frame=%dMB rss=%dMB rss_now=%dMB cad=%.0fs"
                          % (len(evs), c.max_frame // 1048576, _rss_mb(),
                             _rss_now_mb(), cad))
                    if c.max_frame > LIVE_FRAME_TRIP:
                        # UNA lectura ya pagó el parseo gigante: no repetirlo
                        # cada 2 s — apagar y marcar la sesión para los
                        # próximos turnos (el compact la limpia)
                        events_off = True
                        _live_skip_mark(sid)
                        pwlog("live events OFF: frame de %dMB con %d evs "
                              "(replay con imágenes) — flag %dh por sesión"
                              % (c.max_frame // 1048576, len(evs),
                                 LIVE_SKIP_TTL // 3600))
                if len(evs) > EVENTS_TRIP:
                    events_off = True
                    pwlog("live events OFF: replay %d evs (anti-OOM) — "
                          "sigo con pasos+documento" % len(evs))
                else:
                    # buffer re-enviado entero en cada llamada: solo los NUEVOS
                    for ev in evs[-400:]:
                        eid = ev.get("eventId")
                        if not eid or eid in vistos:
                            continue
                        vistos.add(eid)
                        if len(json.dumps(ev)) > 30000:  # snapshot: ignorar
                            continue
                        p = ev.get("payload") or {}
                        k = p.get("kind") or p.get("type") or ""
                        if k == "reasoning_delta" and p.get("delta"):
                            d = p["delta"]
                            razon = (razon + d)[-8192:]  # cola: el íntegro
                            razon_n += len(d)             # vive en cot_full
                            cot_full += d
                            n_deltas += 1
                        elif k == "text_delta" and p.get("delta"):
                            d = p["delta"]
                            texto = (texto + d)[-8192:]   # cola para la caja
                            texto_n += len(d)             # viva; el documento
                            n_deltas += 1                 # final sale del store
                            if razon and not evento_write:
                                # EVENTO DE FASE: pasó de pensar a escribir —
                                # mensaje propio, abajo de las cajas de CoT
                                evento_write = True
                                if cfg.get("cot_stream", True) and not segundo_plano:
                                    send(cfg, "🧠 <b>«%s»</b> — análisis "
                                              "terminado (%s chars de CoT · "
                                              "%d cajas)\n✍️ ahora escribiendo "
                                              "la respuesta…"
                                              % (esc(titulo),
                                                 fmt_tok(len(cot_full)),
                                                 cot_nmsg), loud=False)
                            razon = ""  # empezó a escribir: el CoT ya cumplió
        except Exception:  # noqa: BLE001 - el en vivo jamás rompe el turno
            pass
        # pasos: cada PASO del turno es un mensaje assistant que completa —
        # poll liviano del historial nuevo (afterMessageId) cada 6 s
        if time.time() - last_poll_pasos >= 6.0:
            last_poll_pasos = time.time()
            try:
                nuevos = _fetch_messages(c, sid, after_id=pasos_last, limit=30)
                if nuevos:
                    pasos_last = _last_id_of(nuevos) or pasos_last
                    pasos.extend(_steps_of_messages(nuevos))
                    # F22bis: cajas PERMANENTES de cada tool call nuevo
                    # (comando completo / diff) — como las de CoT.
                    # `nuevos` ya es incremental (after_id): son todas nuevas
                    if not segundo_plano:
                        try:
                            _cajas_paso_nuevas(
                                cfg, titulo, pasos_nmsg,
                                _tools_of_messages(nuevos))
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                pass
        if live_id and not segundo_plano and time.time() - last_edit >= 3.0:
            last_edit = time.time()
            # cajas PERMANENTES de CoT: lo que ya no cabe en la cola viva
            # se manda como mensaje fijo (scrolleable de principio a fin)
            cot_flush(COT_TAIL)
            trans = int(time.time() - t0)
            if texto:
                cuerpo = ("⚙️ <b>«%s»</b> · %ds\n<pre>%s</pre>\n✍️ escribiendo…"
                          % (esc(titulo), trans, esc(texto[-3200:])))
            elif events_off:
                cuerpo = ("📄 <b>«%s»</b> · %ds\nCoT en vivo pausado "
                          "(sesión muy grande para el stream — protege la "
                          "memoria del bot; /compact lo reactiva).\n"
                          "🔧 pasos activos · el documento "
                          "final trae el CoT COMPLETO."
                          % (esc(titulo), trans))
            elif razon:
                cuerpo = ("🧠 <b>«%s»</b> · %ds\n<b>pensando:</b>\n<pre>%s</pre>"
                          % (esc(titulo), trans, esc(razon[-3200:])))
            elif n_deltas == 0 and trans > 12:
                # sin deltas = el turno lo ejecuta la app de la Mac (task
                # abierta ahí): su streaming interno no cruza al bot
                cuerpo = ("🖥️ <b>«%s»</b> · %ds\ncorriendo en la Mac "
                          "(task abierta en la app) — te aviso al terminar"
                          % (esc(titulo), trans))
            else:
                cuerpo = ("🧠 <b>«%s»</b> · %ds\npensando…"
                          % (esc(titulo), trans))
            _live_edit(cfg, msg_id, cuerpo)
            if pasos:
                if ph_pasos is None:
                    ph_pasos = _live_send(cfg, "🔧 <b>«%s»</b> — pasos:"
                                          % esc(titulo))
                if ph_pasos:
                    ult = pasos[-12:]
                    _live_edit(cfg, ph_pasos,
                               "🔧 <b>«%s»</b> — %d pasos:\n%s"
                               % (esc(titulo), len(pasos),
                                  "\n".join(
                                      "• %s%s" % (esc(n), " · " + esc(d) if d else "")
                                      for n, d in ult)
                                  + ("\n···" if len(pasos) > 12 else "")))
        time.sleep(cad)  # adaptativa: 2 s normal, 5 s con frame grande
    # fin del turno: el CoT restante también a mensajes permanentes —
    # el chat queda con el razonamiento COMPLETO scrolleable
    if not segundo_plano:  # F15: backgrounded = sin lluvia de cierre
        try:
            cot_flush(0, pace=False)
        except Exception:  # noqa: BLE001
            pass
    pwlog("live fin: deltas=%d razon=%d texto=%d pasos=%d cot_msgs=%d"
          % (n_deltas, razon_n, texto_n, len(pasos), cot_nmsg))
    # [1] es la COLA del texto (8KB) — el texto íntegro sale del historial
    return cot_full, texto, pasos, live_id


def _spawn_worker(cfg, act, text):
    """F16: lanza el worker como PROCESO detached (start_new_session=True,
    patrón del stuck-watch). Un kickstart/restart/crash del poller NO lo
    toca: el turno vive su ciclo completo y se reporta solo por Telegram.
    Payload JSON por stdin (argv se queda corto para prompts de 6KB)."""
    if _nuclear_active():
        # Nuclear 2.0 — cerradura: NADA nuevo arranca durante el ciclo
        # (antes el poller podía spawnear un worker a mitad del nuclear
        # que moría con el kickstart sin reporte). La cola queda dormida
        # y sobrevive: el auto-resume la drena al volver.
        pwlog("spawn bloqueado: nuclear en curso")
        return None
    _save_last_prompt(act, text)  # F22: /retry siempre tiene qué relanzar
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")  # shim: entrypoint de subprocesos
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        with open(os.path.join(LOG_DIR, "worker-stderr.log"), "a") as err:
            p = subprocess.Popen(
                [sys.executable, me, "--run-turn"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                start_new_session=True,
                cwd=act.get("ws") or os.path.expanduser("~"))
            p.stdin.write(json.dumps({"act": act, "text": text}).encode("utf-8"))
            p.stdin.close()
        return p
    except OSError as e:
        send(cfg, "❌ No pude lanzar el worker: " + esc(str(e)[:120]))
        return None


def cmd_run_turn():
    """F16: entrypoint del worker-proceso (--run-turn). Lee {act, text} de
    stdin, instala el SIGTERM → marker (nuclear amable: el shutdown pasa
    por el mismo camino que /stop) y un atexit de seguridad para el
    registry; luego corre el mismo prompt_worker de siempre."""
    import atexit
    import signal
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    act = payload.get("act") or {}
    text = payload.get("text") or ""
    sid = act.get("sid")

    def _sig(signum, frame):
        _stop_request(sid)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (OSError, ValueError):
        pass
    atexit.register(lambda: WORKERS.pop(sid or "nueva"))
    prompt_worker(load_config(), act, text)


def prompt_worker(cfg, act, text):
    """Corre un prompt headless en la conversación activa y empuja la
    respuesta a Telegram. Vive en un PROCESO (F16: actor)."""
    import threading as _th
    sid, ws, titulo = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    key = sid or "nueva"
    abandonado = [False]  # F21: fin=None → el runtime se hereda al supervisor
    if sid and WORKERS.get(sid) is not None:
        # ya hay un worker para la sesión (carrera poller/queue_drain):
        # encolar sin avisar — el que corre hace drain al terminar
        _queue_put(sid, act, text)
        return
    if sid and sid in running_turns():
        # F17: la task está ocupada (corrida de la Mac, sin worker nuestro)
        # → cola DORMIDA, sin spawnnear runtime (el relay en loop gastaba
        # ~500MB por ciclo). La despierta quien termina: on_stop de la Mac
        # o el drain del worker.
        pwlog("busy -> encolar (task ocupada, sin runtime)")
        _queue_put(sid, act, text)
        send(cfg, "⏳ La Mac está ocupada con «%s» — tu mensaje quedó en "
                  "la cola (%d en espera). Corre al terminar la corrida "
                  "(o /stop para limpiarla)."
                  % (esc(titulo), len(_queue_peek(sid))))
        return
    WORKERS.register(key, {"t0": time.time(), "prompt": text[:80],
                           "ws": ws, "titulo": titulo})
    pwlog("worker start sid=%s pid=%d titulo=%s rss=%dMB texto=%r"
          % (sid, os.getpid(), titulo, _rss_mb(), text[:60]))
    try:
        c = zc_proto.make(cwd=ws,
                          on_user_input=_user_input_handler(cfg, sid, titulo),
                          on_permission=lambda params: (
                               pwlog("🔐 permiso en headless: %s"
                                     % json.dumps(params, ensure_ascii=False)[:200]),
                               send(cfg, "🔐 «%s» pidió un permiso headless "
                                         "(debería correr yolo — avisame para "
                                         "revisarlo): <code>%s</code>"
                                         % (esc(titulo), esc(json.dumps(
                                             params, ensure_ascii=False)[:120])))))
    except OSError as e:
        WORKERS.pop(key)  # OJO key: pop(None) dejaba el orphan "nueva" (bug F0)
        send(cfg, "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120]))
        return
    # F16: /stop pasa por marker (state/bot-stop/<sid>) — el ProtoClient
    # ya no se comparte con el poller (los pipes no cruzan procesos)
    snap_sel = None  # /stop puede frenar ANTES de la asignación (finally la lee)
    stop_ya = []

    def stop_hook():
        """F16: el gancho de _turn_end_poll — si el poller dejó marker de
        /stop, frenar con el runtime PROPIO del worker (stop real)."""
        if stop_ya or not _stop_requested(sid):
            return
        stop_ya.append(1)
        pwlog("stop marker: session/stop %s" % sid)
        try:
            c.request("session/stop", {"sessionId": sid}, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    try:
        if not sid:  # /task new: crear la sesión AHORA (vive durante el turno)
            sid, err = _create_session(c, ws)
            if err:
                send(cfg, err)
                return
            act = dict(act, sid=sid)
            set_active_bot_session(act)
            WORKERS.rekey("nueva", sid)  # el botón de /stop apunta al sid real
        modelo = act.get("model")
        snap_sel = None if sid in (None, "nueva") else _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot: nada que restaurar
        err = _proto_ready(c, sid, model_sel=modelo, mode=act.get("mode"))
        if err:
            send(cfg, err)
            return
        if _stop_requested(sid):
            # /stop llegó durante el arranque: ni mandar el turno
            send(cfg, "⏹️ «%s» frenado por /stop (antes de enviar)." % esc(titulo))
            return
        pre = _fetch_messages(c, sid, limit=1)
        last_id = _last_id_of(pre)
        t0 = time.time()
        r = c.request("session/send",
                      {"sessionId": sid, "content": _headless_text(text, cfg),
                       "modelSelection": modelo or MODEL_SEL}, timeout=120)
        pwlog("send resp:", json.dumps(r)[:200])
        if "error" in r:
            send(cfg, "❌ send: " + esc(json.dumps(r["error"])[:150]))
            return
        t_send = time.time()  # F20: ancla del turnId PROPIO (turno cruzado)
        _active_turn_register(sid, titulo, t0, ws=ws, last_id=last_id)
        # Vista en vivo por delta stream: suscribir ANTES del send para
        # captar el razonamiento desde el arranque (reasoning_delta).
        ph = None
        if cfg.get("live_progress", True):
            ph = _live_send(cfg, "🧠 <b>«%s»</b> arrancando…" % esc(titulo))
            if ph:
                try:
                    c.request("session/subscribe",
                              {"sessionId": sid,
                               "deliveryKind": "desktop-continuous"}, timeout=15)
                except Exception:  # noqa: BLE001
                    pass
        if ph:
            res = {}
            th = _th.Thread(target=lambda: res.update(
                fin=_turn_end_poll(sid, t0, timeout=1800,
                                   stop_check=stop_hook, t_send=t_send)),
                daemon=True)
            th.start()
            lr = _live_view(c, cfg, sid, t0, titulo, ph, th, last_id=last_id)
            live_cot = (lr[0] if lr else "") or ""
            live_box = (lr[3] if lr else None) or ph  # caja viva (abajo si hubo CoT)
            fin = res.get("fin")
            if fin is None:  # la vista cortó por deadline: esperar el resto
                fin = _turn_end_poll(sid, t0, timeout=600, stop_check=stop_hook,
                                     t_send=t_send)
        else:
            fin = _turn_end_poll(sid, t0, timeout=1800, stop_check=stop_hook,
                                 t_send=t_send)
            live_cot = ""
            live_box = ph
        pwlog("fin:", (fin or {}).get("event"), "rss=%dMB rss_now=%dMB"
              % (_rss_mb(), _rss_now_mb()))
        REINTENTA = {"inline_keyboard": [[
            {"text": "🔁 Reintentar", "callback_data": "retry"}]]}
        if ph:
            if fin is None:
                _live_edit(cfg, live_box, "⏱️ <b>«%s»</b> — turno largo"
                           % esc(titulo))
            elif fin.get("event") == "turn.failed":
                _live_edit(cfg, live_box, "❌ <b>«%s»</b> falló — detalle abajo"
                           % esc(titulo))
            else:
                _live_edit(cfg, live_box, "✅ <b>«%s»</b> · respuesta:" % esc(titulo))
        if fin is None:
            # F21 — fin honesto: el turno REALMENTE sigue (el runtime vive,
            # el CLI sigue escribiendo al log). Antes el worker moría acá y
            # su finally MATABA el runtime con el mensaje "sigue de fondo"
            # en pantalla: mentira + turno muerto. Ahora:
            # 1) dejo el runtime VIVO (no lo cerramos en finally)
            # 2) registro el turno como huérfano: el supervisor del poller
            #    (_orphans_check) lo vigila y entrega la respuesta al final
            pwlog("abandono: fin=None tras 40min — runtime vivo, supervisor "
                  "asume la entrega")
            _orphans_handoff(cfg, sid, titulo, c, last_id, ph, live_box)
            raise _TurnAbandoned()  # salta al finally SIN cerrar el runtime
        if fin.get("event") == "turn.failed":
            err = str((fin.get("error") or {}).get("message", ""))[:150]
            send(cfg, "❌ «%s» falló: %s" % (esc(titulo), esc(err)),
                 markup=REINTENTA)
            return
        if WORKERS.stopped(sid):
            if ph:
                _live_edit(cfg, live_box, "⏹️ <b>«%s»</b> frenado" % esc(titulo))
            send(cfg, "⏹️ «%s» frenado por /stop." % esc(titulo))
            return
        dur = (time.time() - t0) / 60.0
        msgs = _fetch_messages(c, sid, after_id=last_id, limit=120) or []
        # F20: SOLO el último mensaje assistant — en sesión compartida con
        # la app hay respuestas ajenas en la ventana (incidente 18:29)
        texto = _extract_assistant_text(msgs, solo_ultima=True).strip()
        _turn_trace(cfg, titulo, sid, dur, msgs, live_cot=live_cot)
        if not texto:
            send(cfg, "✅ «%s» listo en %.0f min (sin texto de respuesta)." % (esc(titulo), dur))
            return
        _send_long_reply(cfg, titulo, dur, texto)
    except _TurnAbandoned:
        abandonado[0] = True
        # el mensaje al chat ya lo mandó _orphans_handoff; nada más acá
    except Exception as e:
        # un worker muerto NUNCA más es silencioso: avisar al chat + log
        import traceback
        pwlog("worker CRASH:", traceback.format_exc()[-500:])
        try:
            send(cfg, "⚠️ El worker de «%s» murió procesando tu mensaje: "
                  "<code>%s</code>" %
                  (esc(titulo), esc(str(e)[:150])), loud=True,
                  markup={"inline_keyboard": [[
                      {"text": "🔁 Reintentar", "callback_data": "retry"}]]})
        except Exception:
            pass
    finally:
        if abandonado[0]:
            # F21: el registro de turno activo QUEDA (es la señal del
            # supervisor); el runtime no se toca; la cola la maneja el
            # supervisor al fin real del turno.
            pwlog("worker out (runtime heredado al supervisor)")
            return
        _active_turn_clear(sid)
        WORKERS.pop(sid or "nueva")
        if sid:
            _stop_consume(sid)  # F16: marker consumido — no frenar el próximo turno
        try:
            c.close()
        except Exception as e:
            log("errors", "worker close: %s" % e)
        if sid and snap_sel:
            try:
                _restore_model(sid, snap_sel)
            except Exception as e:
                log("errors", "worker restore: %s" % e)
        try:
            _queue_drain(cfg, sid)
        except Exception:
            pass


def _queue_drain(cfg, sid):
    """Al terminar un turno, si quedaron prompts encolados para la sesión,
    lanza el siguiente (cadena). F17: si la sesión SIGUE ocupada (corrida
    de la Mac), la cola queda DORMIDA — sin este guard el relay
    re-spawneaba en loop mientras la task estaba ocupada (incidente
    18:03: 5 workers en 5 s, cada uno con su runtime). F15: si dejó de
    ser la task current, también duerme (la retoma /task)."""
    if not sid:
        return
    if _nuclear_active():
        pwlog("drain skip: nuclear en curso (cola dormida: %d)"
              % len(_queue_peek(sid)))
        return
    if sid in running_turns():
        pwlog("drain skip: %s sigue ocupada (cola dormida: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    if not _task_current_ok(cfg, sid):
        pwlog("drain skip: %s no es la task current (cola dormida: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    _queue_launch_next(cfg, sid)


def _queue_peek(sid):
    q = _store("bot-queue.json").read()
    return (q.get(sid) or []) if isinstance(q, dict) else []


def _queue_sids_with_items():
    q = _store("bot-queue.json").read()
    if not isinstance(q, dict):
        return []
    return [sid for sid, lst in q.items() if lst]


# ---------------------------------------- turnos activos del bot (F21)

def _active_turn_register(sid, titulo, t0, ws=None, last_id=None):
    """Registro persistente del turno en curso: si el worker muere SIN
    correr su finally (kill -9 / OOM), el supervisor del poller lo ve y
    entrega el final — nadie muere en silencio."""
    if not sid:
        return

    def add(q):
        q = q if isinstance(q, dict) else {}
        q[sid] = {"titulo": titulo, "t0": t0, "pid": os.getpid(),
                  "ws": ws, "last_id": last_id}
        return q
    try:
        _store("bot-active.json").update_cross_process(add, {})
    except OSError:
        pass


def _active_turn_clear(sid):
    if not sid:
        return

    def rm(q):
        q = q if isinstance(q, dict) else {}
        q.pop(sid, None)
        return q
    try:
        _store("bot-active.json").update_cross_process(rm, {})
    except OSError:
        pass


def _runtime_muerto(c):
    """True si el runtime hijo del worker ya no existe (OOM/kill)."""
    p = getattr(c, "p", None)
    try:
        return p is not None and p.poll() is not None
    except OSError:
        return True


class _TurnAbandoned(Exception):
    """F21: fin=None tras 40 min — el turno real sigue pero el worker
    entrega el runtime al supervisor (nunca matarlo ni mentir)."""


def _orphan_fetch_text(ent, sid):
    """F21: texto de la respuesta de un turno huérfano ya completado.
    Runtime efímero: resume del sid y fetch — SIN setModel/setMode para
    no pisar la configuración de la sesión. Nunca lanza."""
    try:
        c = zc_proto.make(cwd=ent.get("ws") or DEFAULT_WS)
        try:
            r = c.request("session/resume", {"sessionId": sid}, timeout=45)
            if "error" in r:
                return None
            msgs = _fetch_messages(c, sid, after_id=ent.get("last_id"),
                                   limit=120) or []
            return _extract_assistant_text(msgs, solo_ultima=True).strip()
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None


def _orphans_handoff(cfg, sid, titulo, c, last_id, ph, live_box):
    """F21: registro del turno activo para el supervisor del poller. El
    runtime queda VIVO (proceso worker lo pierde al salir, pero el runtime
    es hijo del worker... OJO: al morir el worker, el runtime queda
    huérfano PERO SIGUIENDO — el CLI escribe al log del CLI, que es lo
    único que el supervisor necesita leer. La entrega del texto la hace
    _orphans_check al ver turn.completed)."""
    _active_turn_register(sid, titulo, time.time())
    if ph:
        try:
            _live_edit(cfg, live_box,
                       "⏱️ <b>«%s»</b> — turno largo: sigue corriendo y te "
                       "aviso al terminar (supervisión activada)"
                       % esc(titulo))
        except Exception:  # noqa: BLE001
            pass
    send(cfg, "⏱️ «%s» lleva 40 min corriendo — sigo supervisándolo y te "
              "entrego la respuesta apenas termine." % esc(titulo))


def _orphans_check(cfg):
    """F21 — supervisor de huérfanos (corre en cada vuelta del poller):
    turnos del bot cuyo worker murió abruptamente. El finally del worker
    siempre limpia su registro → entrada con pid muerto = muerte SIN
    cleanup → aviso honesto en vez de silencio eterno. Barato: un read +
    un os.kill por entrada."""
    q = _store("bot-active.json").read()
    if not isinstance(q, dict) or not q:
        return
    ahora = time.time()
    for sid, ent in list(q.items()):
        if not isinstance(ent, dict):
            continue
        t0 = float(ent.get("t0") or 0)
        if ahora - t0 > 7200:  # 2h: purge silencioso (anti PID-reuse)
            _active_turn_clear(sid)
            continue
        pid = ent.get("pid")
        try:
            os.kill(int(pid), 0)
            continue  # vivo: worker sano, nada que hacer
        except (OSError, ValueError, TypeError):
            pass
        # pid muerto: ¿llegó a terminar el turno antes de caer el worker?
        fin = None
        for _ts, _s, _ev, d in zc_logs.iter_events(
                BASE, needles=("turn.completed", "turn.failed"),
                sids={sid}, since_epoch=t0, last_files=1,
                tail_bytes=512 * 1024):
            fin = d
        titulo = ent.get("titulo") or sid[-8:]
        if fin is not None and fin.get("event") == "turn.completed":
            # entrega REAL del texto: runtime efímero de solo-lectura
            # (resume del sid SIN setModel — no pisa la configuración)
            texto = _orphan_fetch_text(ent, sid)
            if texto:
                _send_long_reply(
                    cfg, titulo, (ahora - t0) / 60.0, texto)
                pwlog("orphan", sid[-8:], "entregado (%d chars)" % len(texto))
            else:
                send(cfg, "🛟 El worker de «%s» murió, pero su turno "
                      "TERMINÓ — la respuesta quedó en la sesión (/cot "
                      "para el trace)." % esc(titulo))
                pwlog("orphan", sid[-8:], "fin=completed (texto no recuperable)")
        elif fin is not None:
            send(cfg, "❌ «%s» falló (el worker había muerto): %s"
                 % (esc(titulo), esc(str((fin.get("error") or {})
                                         .get("message", ""))[:150])))
            pwlog("orphan", sid[-8:], "fin=failed")
        else:
            send(cfg, "⛔️ El turno de «%s» murió con su worker (kill/OOM) — "
                  "no va a llegar respuesta. Reenviá el mensaje cuando "
                  "quieras." % esc(titulo))
            pwlog("orphan", sid[-8:], "sin fin (runtime muerto)")
        _active_turn_clear(sid)
        # la cola de esa sesión quedó dormida: despertarla si es la current
        try:
            if _task_current_ok(cfg, sid):
                _queue_resume(cfg, sid, titulo)
        except Exception:  # noqa: BLE001
            pass


def _queue_launch_next(cfg, sid):
    """Toma el próximo prompt encolado de la sesión y lanza su worker
    (F16: proceso detached — la cadena de cola sobrevive restarts del
    poller). Devuelve (item, quedan) o None si no había nada."""
    lst = _queue_take(sid)
    if not lst:
        return None
    item = lst[0]
    for resto in lst[1:]:  # los devolvemos a la cola
        _queue_put(sid, resto["act"], resto["text"])
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        _queue_put(sid, item["act"], item["text"])  # no perderlo
        return None
    return item, len(lst) - 1


def _queue_resume(cfg, sid, titulo=None):
    """F15 — auto-resume: al volver una task (con /task) que tenía prompts
    encolados, arranca el primero. Si ya hay un worker vivo de esa sesión,
    la cola sigue sola al terminar su turno."""
    if not sid:
        return None
    if _nuclear_active():
        pwlog("resume skip: nuclear en curso")
        return None
    if WORKERS.get(sid) is not None:
        n = len(_queue_peek(sid))
        if n:
            return ("⏳ Ya hay un turno corriendo en «%s» — tus %d encolados "
                    "corren al terminar." % (esc(titulo or sid[-8:]), n))
        return None
    res = _queue_launch_next(cfg, sid)
    if not res:
        return None
    quedan = res[1]
    return ("⏳ Retomé la cola de «%s»%s — trabajando…"
            % (esc(titulo or sid[-8:]),
               " (%d más encolados)" % quedan if quedan else ""))


def _send_long_reply(cfg, titulo, dur_min, texto, max_partes=3):
    """Respuesta completa troceada en mensajes de ~3900 chars (máx 3 +
    aviso con /last para el resto). Guarda el texto completo para /last."""
    CH = 3900
    total = len(texto)
    partes = [texto[i:i + CH] for i in range(0, total, CH)]
    try:
        with open(os.path.join(BASE, "hooks", "state", "last-reply.txt"), "w") as f:
            f.write(texto)
    except OSError:
        pass
    if len(partes) > max_partes:
        resto = sum(len(p) for p in partes[max_partes:])
        partes = partes[:max_partes]
        cola = "\n\n(+%d caracteres más — mandá /last)" % resto
    else:
        cola = ""
    for i, p in enumerate(partes):
        pre = ("✅ «%s» listo en %.0f min" % (esc(titulo), dur_min)) if i == 0 else "···"
        suf = " (%d/%d)" % (i + 1, len(partes)) if len(partes) > 1 else ""
        send(cfg, pre + suf + "\n" + esc(p) + (cola if i == len(partes) - 1 else ""),
             loud=False)


def register_bot_menu(cfg):
    """Menú de comandos de Telegram (botón '/'): se re-registra al arrancar
    el poller para mantenerlo al día."""
    cmds = [
        ("task", "conversaciones con Project · /task n elige la activa"),
        ("all_tasks", "todas las conversaciones, incluidas las sueltas"),
        ("model", "modelo de la conversación activa (botones)"),
        ("thinking", "nivel de razonamiento: max · high · low"),
        ("mode", "Plan mode 🗺 · Full access 🔓 (botones)"),
        ("status", "qué está corriendo + conversación activa"),
        ("queue", "mensajes en cola (lanzar ahora / descartar)"),
        ("retry", "relanzar el último prompt (tras un fallo)"),
        ("ctx", "contexto usado por cada task"),
        ("compact", "elegir task para compactar (Projects)"),
        ("all_compact", "ídem, incluidas las sueltas del grupo Tasks"),
        ("run", "nueva tarea y mandar prompt (/run proyecto prompt)"),
        ("stop", "frena la corrida y LIMPIA su cola de mensajes"),
        ("nuclear", "emergencia: frena TODO (Mac incluida) y reinicia"),
        ("quota", "credits del coding plan"),
        ("usage", "consumo de hoy por sesión"),
        ("last", "reenviar la última respuesta completa"),
        ("cot", "paso a paso + razonamiento del turno (documento)"),
        ("help", "todos los comandos"),
    ]
    try:
        params = {"commands": [{"command": c, "description": d} for c, d in cmds]}
        if cfg.get("chat_id"):
            # D1: menú visible SOLO en el chat del dueño (scope oficial)
            params["scope"] = {"type": "chat", "chat_id": cfg["chat_id"]}
        tg_api(cfg, "setMyCommands", params)
        return True
    except Exception as exc:  # noqa: BLE001
        log("errors", "setMyCommands: %s" % exc)
        return False


def handle_free_text(cfg, text):
    """Texto libre fuera de esperas: si hay una ❓ headless pendiente la
    responde; si no, va a la conversación activa (headless)."""
    import threading as _th
    # 1) respuesta a una pregunta del agente headless (F15: prioriza la de
    # la task current; fallback a la única pendiente) — F16: por archivo
    act = active_bot_session()
    pend = QUESTIONS.first_pending((act or {}).get("sid"))
    if pend is not None:
        resp = text.strip()
        qs = pend.get("questions") or []
        # numero -> label/value de la opcion
        if resp.isdigit() and qs:
            try:
                ops = (qs[0].get("options") or [])
                op = ops[int(resp) - 1]
                resp = op.get("value") or op.get("label") or resp
            except (IndexError, ValueError):
                pass
        QUESTIONS.answer(pend.get("request_id"), resp)
        return "✅ Respondido: %s" % esc(resp[:200])
    if not act:
        return ("💬 Todavía no hay conversación activa — mandá /task y elegí "
                "una con /task n.\nDespués, todo texto que mandes corre ahí "
                "(headless, pantalla bloqueada OK).")
    sid = act.get("sid")
    if sid and sid in running_turns():
        # F17: la task está ocupada (Mac o turno propio) → encolar INLINE,
        # sin spawnnear NADA: un aviso claro por mensaje y cola dormida.
        # Antes el worker spawneaba un runtime de ~500MB para descubrir
        # lo mismo (y el relay lo hacía en loop).
        _queue_put(sid, act, text)
        return ("⏳ La Mac está ocupada con «%s» — tu mensaje quedó en la "
                "cola (%d en espera). Corre al terminar la corrida "
                "(o /stop para limpiarla)."
                % (esc(act.get("title", "?")), len(_queue_peek(sid))))
    if _spawn_worker(cfg, act, text) is None:
        return None  # el error ya salió por el chat
    return "▶️ «%s» — trabajando (headless)…" % esc(act.get("title", "?"))


def status_report():
    act = active_bot_session()
    lines = ["📊 <b>Estado</b>"]
    if act:
        tag = os.path.basename(act.get("ws") or "")
        lines.append("🎯 Activa: «%s»%s" % (esc(act.get("title", "")),
                                           " · 📁" + esc(tag) if tag else ""))
        # F22: configuración de la activa a la vista
        sel = _cur_sel(act)
        lines.append("🤖 %s · 🧠 %s · 🎛 %s"
                     % (esc(sel.get("modelId", "?")),
                        esc((sel.get("options") or {}).get("reasoningLevel",
                                                           "high")),
                        esc(_modo_label(act.get("mode") or "yolo"))))
    else:
        lines.append("🎯 Activa: ninguna (/task n para elegir)")
    workers = WORKERS.items()
    if workers:
        lines.append("🤖 <b>Workers del bot</b>:")
        for sid, w in workers:
            mins = max(0, int((time.time() - (w.get("t0") or time.time())) / 60))
            lines.append("  • «%s» pid %d · %d min · %s"
                         % (esc(w.get("titulo") or sid[-8:]), w.get("pid") or 0,
                            mins, esc(preview(w.get("prompt") or "", 50))))
    for sid in _queue_sids_with_items():
        if WORKERS.get(sid) is None:  # colas dormidas (las vivas las ve arriba)
            n = len(_queue_peek(sid))
            act_sid = (act or {}).get("sid")
            marca = " 🎯" if act and sid == act_sid else ""
            lines.append("📬 Cola dormida «%s»%s — %d mensaje%s (/queue)"
                         % (esc(sid[-8:]), marca, n, "s" if n != 1 else ""))
    activos = running_turns()
    if activos:
        for sid, ts in activos.items():
            if WORKERS.get(sid) is not None:
                continue  # ya listado como worker con detalle
            titulo = session_title_from_db(sid) or sid[-8:]
            import datetime
            lines.append("🔄 «%s» desde %s (corrida de la Mac)"
                         % (esc(titulo),
                            datetime.datetime.fromtimestamp(ts).strftime("%H:%M")))
    if not workers and not activos and not _queue_sids_with_items():
        lines.append("😌 Nada corriendo.")
    lines.append("🫀 poller %d · arriba %s · RSS %dMB"
                 % (os.getpid(), _uptime_txt(), _rss_now_mb()))
    return "\n".join(lines)


_POLLER_T0 = time.time()


def _last_prompt_path():
    return os.path.join(STATE_DIR, "last-prompt.json")


def _save_last_prompt(act, text):
    try:
        _store("last-prompt.json").write({"act": act, "text": text})
    except OSError:
        pass


def _load_last_prompt():
    d = _store("last-prompt.json").read()
    if isinstance(d, dict) and d.get("text"):
        return d.get("act") or {}, str(d["text"])
    return None, None


def _cmd_retry(cfg, rest):
    """/retry — relanza el último prompt tal cual (fallo, crash o timeout).
    Si hay una task activa distinta a la del prompt, pregunta... no: corre
    en la task ORIGINAL del prompt (donde tiene contexto)."""
    act, text = _load_last_prompt()
    if not text:
        return "🤔 No tengo un prompt previo para reintentar."
    if WORKERS.get((act or {}).get("sid") or "nueva") is not None:
        return "⏳ Ya hay un turno corriendo en «%s» — /stop primero si querés."
    if _spawn_worker(cfg, act, text) is None:
        return None
    return "🔁 Reintentando «%s»:\n%s" % (esc((act or {}).get("title", "?")),
                                          esc(preview(text, 200)))


def _uptime_txt():
    s = int(time.time() - _POLLER_T0)
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


REG = zc_commands.CommandRegistry()  # despacho por tabla (Command Registry)


def _cmd_start(cfg, rest):
    return "🤖 Bot de control de ZCode activo.\nComandos: /quota — /ping — /help"


def _cmd_quota(cfg, rest):
    return quota_report()


def _cmd_usage(cfg, rest):
    return usage_report()


def _cmd_ctx(cfg, rest):
    return context_report()


def _cmd_compact(cfg, rest):
    if not rest:
        return compact_list_report("projects")
    return _compact_args(rest, "projects")


def _cmd_all_compact(cfg, rest):
    if not rest:
        return compact_list_report("all")
    return _compact_args(rest, "all")


def _cmd_stop(cfg, rest):
    """/stop SOLO maneja corridas iniciadas desde el celular: lo que corre
    en la Mac se frena con el botón Stop de la app, acá ni se lista."""
    if rest.startswith("@ses"):
        return bot_stop(rest.split()[0][1:])
    if rest.isdigit():
        items = WORKERS.items()
        k = int(rest)
        if 1 <= k <= len(items):
            sid, w = items[k - 1]
            return bot_stop(sid, w.get("titulo"))
        return "⚠️ Número fuera de la lista (1 a %d) — mandá /stop." % len(items)
    return stop_list_report()


def _cmd_task(cfg, rest):
    if not rest:
        return task_list_report("projects")
    if rest.startswith("@ses"):
        return _task_select_sid(rest[1:])
    if rest.isdigit():
        return task_select(int(rest), "projects")
    if rest.split()[0] == "new":
        tag = rest.split(" ", 1)[1].strip() if " " in rest else ""
        ws = _resolve_ws(tag)
        if not ws:
            return ("⚠️ No conozco el proyecto «%s» — probá sin nombre "
                    "(usa el activo) o el nombre de carpeta." % esc(tag))
        set_active_bot_session({"sid": None, "ws": ws,
                                "title": "nueva · " + os.path.basename(ws)})
        return ("🆕 Nueva conversación en %s — mandá el primer mensaje "
                "y arranca (headless)." % esc(os.path.basename(ws)))
    return ("⚠️ /task lista · /task &lt;n&gt; elige · /task new [proyecto] · "
            "/all_tasks muestra todas")


def _cmd_all_tasks(cfg, rest):
    if rest.isdigit():
        return task_select(int(rest), "all")
    return task_list_report("all")


def _cmd_status(cfg, rest):
    return status_report()


def _cmd_last(cfg, rest):
    try:
        with open(os.path.join(BASE, "hooks", "state", "last-reply.txt"),
                  errors="replace") as f:
            texto = f.read()
    except OSError:
        return "🤷 No hay respuesta previa guardada."
    if not texto.strip():
        return "🤷 La última respuesta estaba vacía."
    _send_long_reply(cfg, "respuesta completa", 0, texto, max_partes=10)
    return None


def _cmd_cot(cfg, rest):
    """/cot [n|@sid] — paso a paso + razonamiento completo de una task como
    documento scrolleable. Sin args: la activa. Funciona TAMBIÉN para tasks
    corridas en la app de la Mac (leer el historial sí cruza runtimes —
    lo que no cruza es frenar)."""
    import datetime
    tasks = {t["sid"]: t for t in ctx_tasks(30)}
    target = None
    r = (rest or "").strip()
    if r.startswith("@ses"):
        target = tasks.get(r.split()[0][1:])
        if target is None:
            return "⚠️ No encontré esa conversación — mandá /task."
    elif r.isdigit():
        lst = _tasks_scope("all", 20)
        k = int(r)
        if not 1 <= k <= len(lst):
            return "⚠️ Número fuera de la lista (1 a %d) — /all_tasks." % len(lst)
        target = lst[k - 1]
    else:
        act = active_bot_session()
        if act and act.get("sid"):
            target = tasks.get(act["sid"])
        if target is None:
            return "⚠️ Elegí conversación primero (/task n) o pasá su número."
    try:
        c = zc_proto.make(cwd=target["ws"])
    except OSError as e:
        return "❌ " + esc(str(e)[:100])
    try:
        rr = c.request("session/resume", {"sessionId": target["sid"]}, timeout=60)
        if "error" in rr:
            return "❌ resume: " + esc(json.dumps(rr["error"])[:150])
        msgs = _fetch_messages(c, target["sid"], limit=150) or []
        trace = build_trace(target["title"] or target["sid"][-8:],
                            target["sid"], 0.0, msgs)
        try:
            with open(os.path.join(STATE_DIR, "last-turn-trace.txt"), "w") as f:
                f.write(trace)
        except OSError:
            pass
        fname = "cot-%s-%s.txt" % (
            re.sub(r"[^A-Za-z0-9._-]+", "-",
                   (target["title"] or "ses")[:24]).strip("-"),
            datetime.datetime.now().strftime("%H%M"))
        ok = send_document(cfg, fname, trace,
                           "🧠 Trace de «%s»: %d pasos · últimos %d mensajes"
                           % (target["title"] or target["sid"][-8:],
                              len(_steps_of_messages(msgs)), len(msgs)))
        return ("📄 Documento enviado." if ok else
                "⚠️ No pude mandar el documento — reintentá /cot.")
    finally:
        c.close()


def _cmd_run(cfg, rest):
    import threading as _th
    if not rest:
        return "⚠️ /run [proyecto] &lt;prompt&gt; — crea una conversación nueva y manda el prompt."
    partes = rest.split(" ", 1)
    if len(partes) == 2 and _resolve_ws(partes[0]) and " " in rest:
        ws, prompt = _resolve_ws(partes[0]), partes[1].strip()
    else:
        ws, prompt = active_workspace_path(), rest
    if not prompt:
        return "⚠️ Falta el prompt."
    act = {"sid": None, "ws": ws, "title": "run · " + os.path.basename(ws or "")}
    _spawn_worker(cfg, act, prompt)
    return None


def _cmd_model(cfg, rest):
    act = active_bot_session()
    if not act:
        return "⚠️ Elegí conversación primero (/task n o /task new)."
    sel = _cur_sel(act)
    titulo = act.get("title") or "?"
    mid = sel.get("modelId", "?")
    th = (sel.get("options") or {}).get("reasoningLevel", "high")
    if not rest:
        fila = [{"text": ("● " if mid == m else "○ ") + m,
                 "callback_data": "m:" + m} for m in MODELOS]
        return ("🎯 «%s»\n🤖 Modelo: %s · 🧠 thinking: %s\n"
                "Tocá para cambiar (aplica en el próximo turno):"
                % (esc(titulo), esc(mid), esc(th)),
                {"inline_keyboard": [fila]})
    if rest not in MODELOS:
        return "⚠️ Modelos: %s" % " · ".join(MODELOS)
    _set_sel(act, model_id=rest)
    return ("🎯 «%s»\n🤖 Modelo: %s · 🧠 thinking: %s (aplica en el próximo turno)"
            % (esc(titulo), esc(rest), esc(th)))


def _cmd_thinking(cfg, rest):
    act = active_bot_session()
    if not act:
        return "⚠️ Elegí conversación primero (/task n o /task new)."
    sel = _cur_sel(act)
    titulo = act.get("title") or "?"
    mid = sel.get("modelId", "?")
    th = (sel.get("options") or {}).get("reasoningLevel", "high")
    if not rest:
        fila = [{"text": ("● " if th == lv else "○ ") + lv,
                 "callback_data": "th:" + lv} for lv in _thinking_levels(mid)]
        return ("🎯 «%s» · 🤖 %s\n🧠 Thinking: %s\n"
                "Tocá para cambiar (aplica en el próximo turno):"
                % (esc(titulo), esc(mid), esc(th)),
                {"inline_keyboard": [fila]})
    niveles = _thinking_levels(mid)
    lv = rest.lower()
    if lv not in niveles:
        return "⚠️ Niveles: %s" % " · ".join(niveles)
    _set_sel(act, thinking=lv)
    return ("🎯 «%s»\n🧠 Thinking: %s · modelo %s (aplica en el próximo turno)"
            % (esc(titulo), esc(lv), esc(mid)))


MODOS = (("plan", "🗺 Plan mode"), ("yolo", "🔓 Full access"))


def _modo_label(mode):
    return dict(MODOS).get(mode or "yolo", mode or "yolo")


def _cmd_mode(cfg, rest):
    """/mode — dos opciones nada más (pedido de Damian 2026-10-13):
    Plan mode (lectura/plan, nada se toca) y Full access (yolo: disco
    completo sin frenos). El valor viaja igual al protocolo (setMode)."""
    act = active_bot_session()
    if not act:
        return "⚠️ Elegí conversación primero (/task n)."
    modos = tuple(m for m, _ in MODOS)
    actual = act.get("mode") or "yolo"
    if not rest:
        fila = [{"text": ("● " if actual == m else "○ ") + label,
                 "callback_data": "mo:" + m} for m, label in MODOS]
        return ("🎯 «%s»\n🎛 Modo: %s\nTocá para cambiar (aplica en el "
                "próximo turno):" % (esc(act.get("title") or "?"),
                                     esc(_modo_label(actual))),
                {"inline_keyboard": [[b] for b in fila]})
    if rest not in modos:
        return "⚠️ Modos: " + " · ".join(modos)
    set_active_bot_session(dict(act, mode=rest))
    return ("🎛 Modo de «%s»: %s (aplica en el próximo turno)"
            % (esc(act.get("title") or "?"), esc(_modo_label(rest))))


def _cmd_fork(cfg, rest):
    act = active_bot_session()
    if not act or not act.get("sid"):
        return "⚠️ Elegí conversación primero (/task n)."
    try:
        c = zc_proto.make(cwd=act["ws"])
    except OSError as e:
        return "❌ " + esc(str(e)[:100])
    try:
        r = c.request("session/fork", {"sessionId": act["sid"]}, timeout=60)
        if "error" in r:
            return "❌ fork: " + esc(json.dumps(r["error"])[:150])
        res = r.get("result") or {}
        nsid = res.get("sessionId") or (res.get("session") or {}).get("sessionId") or res.get("id")
        if not nsid:
            return "❌ fork sin sessionId: " + esc(json.dumps(res)[:120])
        set_active_bot_session(dict(act, sid=nsid,
                                    title=(act.get("title") or "") + " (fork)"))
        return "🍴 Fork creado y activo."
    finally:
        c.close()


def _cmd_api(cfg, rest):
    if not rest:
        return "⚠️ /api &lt;método&gt; [json de params]"
    partes = rest.split(" ", 1)
    metodo = partes[0]
    params = {}
    if len(partes) == 2 and partes[1].strip():
        try:
            params = json.loads(partes[1])
        except ValueError as e:
            return "⚠️ JSON inválido: %s" % esc(str(e)[:100])
    try:
        c = zc_proto.make(cwd=active_workspace_path())
    except OSError as e:
        return "❌ " + esc(str(e)[:100])
    try:
        r = c.request(metodo, params, timeout=60)
        return "🔌 %s\n%s" % (esc(metodo), esc(json.dumps(r, ensure_ascii=False)[:2800]))
    finally:
        c.close()


def _cmd_ping(cfg, rest):
    return "🏓 pong"


def _cmd_queue(cfg, rest):
    """/queue — el CONTENIDO de las colas (F22): cada encolado con botones
    ⏫ (lanzar ahora) y 🗑 (descartar)."""
    sids = _queue_sids_with_items()
    if not sids:
        return "📭 No hay mensajes encolados."
    lines = ["📬 <b>Cola de prompts</b>"]
    kb = []
    for sid in sids:
        act_sid = (active_bot_session() or {}).get("sid")
        marca = " 🎯" if sid == act_sid else ""
        titulo = session_title_from_db(sid) or sid[-8:]
        lines.append("«%s»%s:" % (esc(titulo), marca))
        for i, item in enumerate(_queue_peek(sid), 1):
            lines.append("  %d. %s" % (i, esc(preview(item.get("text", ""), 80))))
            cb = "%s:%s:%d" % ("x", sid, i - 1)  # base común
            kb.append([
                {"text": "⏫ %d ahora" % i, "callback_data": "qgo:%s:%d" % (sid, i - 1)},
                {"text": "🗑", "callback_data": "qdel:%s:%d" % (sid, i - 1)}])
    return "\n".join(lines), {"inline_keyboard": kb}


def _queue_mutate(sid, fn):
    """F22: read-modify-write ATÓMICO de UNA cola del dict (no pisa las
    demás sesiones). fn(lst) -> nueva lst (o None = sin cambios)."""

    def apply(q):
        q = q if isinstance(q, dict) else {}
        nueva = fn(list(q.get(sid) or []))
        if nueva is None:
            return None
        if nueva:
            q[sid] = nueva
        else:
            q.pop(sid, None)
        return q
    try:
        _store("bot-queue.json").update_cross_process(apply, {})
    except OSError:
        pass


def _cmd_qgo(cfg, rest):
    """/qgo <sid>:<idx> — lanza YA ese encolado (lo saca de la cola)."""
    try:
        sid, idx = rest.rsplit(":", 1)
        idx = int(idx)
    except ValueError:
        return "⚠️ /qgo sid:idx"
    lst = _queue_peek(sid)
    if not (0 <= idx < len(lst)):
        return "🤔 Ese encolado ya no está (¿lo lanzaste dos veces?)."
    item = lst[idx]
    if WORKERS.get(sid) is not None or (sid in running_turns()):
        return "⏳ «%s» sigue ocupada — el turno en curso la drena al final." \
            % esc(sid[-8:])
    taken = [[]]

    def take(l):
        taken[0] = l[idx:idx + 1]
        return l[:idx] + l[idx + 1:]
    _queue_mutate(sid, take)
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        _queue_put(sid, item["act"], item["text"])  # no perderlo
        return None
    return "⏫ Lanzado: %s" % esc(preview(item.get("text", ""), 100))


def _cmd_qdel(cfg, rest):
    """/qdel <sid>:<idx> — descarta ese encolado."""
    try:
        sid, idx = rest.rsplit(":", 1)
        idx = int(idx)
    except ValueError:
        return "⚠️ /qdel sid:idx"
    item = [[]]

    def drop(l):
        if not (0 <= idx < len(l)):
            return None
        item[0] = l[idx]
        return l[:idx] + l[idx + 1:]
    _queue_mutate(sid, drop)
    if not item[0]:
        return "🤔 Ese encolado ya no está."
    return "🗑 Descartado: %s" % esc(preview(item[0].get("text", ""), 100))


def _cmd_help(cfg, rest):
    return ("🤖 <b>Bot unificado ZCode</b> (headless — sirve con la Mac bloqueada)\n"
            "<b>Conversaciones</b>\n"
            "/task — con Project · /task n elige · /task new [proyecto]\n"
            "/all_tasks — todas, incluidas las sueltas del grupo Tasks\n"
            "texto libre — corre en la conversación activa\n"
            "/run [proyecto] prompt — nueva conversación + prompt\n"
            "/model — modelo de la activa · /thinking — max/high/low\n"
            "/mode — Plan mode 🗺 · Full access 🔓 (botones)\n"
            "/fork — bifurcar la activa · /last — última respuesta completa\n"
            "/cot — paso a paso + CoT completo del turno (documento)\n"
            "<b>Control</b>\n"
            "/quota — credits 5h + semanal del plan\n"
            "/usage — detalle de hoy por sesión\n"
            "/ctx — tasks con % de contexto\n"
            "/ping — prueba de vida\n"
            "/status — workers (pid), colas, config de la activa, salud\n"
            "/queue — contenido de las colas (⏫ lanzar / 🗑 descartar)\n"
            "/retry — relanza el último prompt (tras fallo o crash)\n"
            "/stop — frena tu corrida del celular y LIMPIA su cola de mensajes\n"
            "/nuclear — ☢️ emergencia: frena TODO (celular + Mac) y reinicia\n"
            "/deploy — 🚀 corre el pipeline (tests→backup→recarga) con botón\n"
            "/compact — lista Projects para compactar · /all_compact todas\n"
            "(permisos 🔐 y preguntas ❓ se responden tocando los botones, o con texto/número)")


for _names, _fn in [
    (("start",), _cmd_start),
    (("quota",), _cmd_quota),
    (("usage", "uso"), _cmd_usage),
    (("ctx", "contexto"), _cmd_ctx),
    (("compact",), _cmd_compact),
    (("all_compact", "allcompact"), _cmd_all_compact),
    (("stop", "all_stop", "allstop"), _cmd_stop),
    (("nuclear", "nuke"), _cmd_nuclear),
    (("deploy",), _cmd_deploy),
    (("task",), _cmd_task),
    (("all_tasks", "alltasks", "tasks"), _cmd_all_tasks),
    (("status",), _cmd_status),
    (("last",), _cmd_last),
    (("cot", "trace"), _cmd_cot),
    (("run",), _cmd_run),
    (("model",), _cmd_model),
    (("thinking",), _cmd_thinking),
    (("mode",), _cmd_mode),
    (("retry",), _cmd_retry),
    (("queue", "cola"), _cmd_queue),
    (("qgo",), _cmd_qgo),      # callbacks internos de /queue (ocultos)
    (("qdel",), _cmd_qdel),
    (("fork",), _cmd_fork),
    (("api",), _cmd_api),
    (("ping",), _cmd_ping),
    (("help",), _cmd_help),
]:
    REG.register(*_names)(_fn)


def handle_command(cfg, text):
    """Comandos del 2_bot (despacho por Command Registry). Devuelve la
    respuesta (HTML, o (texto, markup)) o None si no es comando."""
    return REG.dispatch(cfg, text)


# ---------------------------------------------------------------- payloads

def first(payload, *keys):
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return None


def preview(value, limit=280):
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def tool_desc(payload):
    name = first(payload, "tool_name", "toolName", "tool") or "herramienta"
    inp = first(payload, "tool_input", "toolInput", "input") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"input": inp}
    return str(name), inp


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def emit(obj):
    """Imprime la salida JSON del hook (schema estricto) y termina."""
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


# ---------------------------------------------------------------- eventos

def marker_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "ses")
    return os.path.join(STATE_DIR, f"turn-{safe}.marker")


# ------------------------------------------- /stop por marker (F16)

def _stop_file(sid):
    d = os.path.join(STATE_DIR, "bot-stop")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sid or "ses")
    return os.path.join(d, safe)


def _stop_request(sid):
    """/stop: el poller deja la orden; el worker la ve en su loop (~2 s)
    y frena SU turno con su propio runtime (el 'stop real': solo el
    runtime dueño ve sus turnos — el ProtoClient compartido murió con los
    threads)."""
    try:
        with open(_stop_file(sid), "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _stop_requested(sid):
    return bool(sid) and os.path.exists(_stop_file(sid))


def _stop_consume(sid):
    try:
        os.remove(_stop_file(sid))
    except OSError:
        pass


def on_user_prompt(cfg, payload):
    prompt = preview(first(payload, "prompt", "text", "message") or "", 200)
    session = str(first(payload, "session_id", "sessionId") or "ses")
    token = f"{time.time_ns()}"
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(marker_path(session), "w") as f:
        f.write(json.dumps({"token": token, "prompt": prompt}))
    # F15: sesión con worker propio → sin stuck-watch (el worker ya tiene su
    # timeout de 30 min + caja viva) y sin ▶️ (doble-stream)
    worker = _worker_owned(session)
    if not worker:
        me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "zcode-telegram.py")  # shim: entrypoint de subprocesos
        if not os.path.exists(me):
            me = os.path.abspath(__file__)
        subprocess.Popen(
            [sys.executable, me, "--stuck-watch", token, session],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if cfg.get("notify_prompt") and _task_current_ok(cfg, session):
            send(cfg, labeled(payload, f"▶️ <b>Turno iniciado</b>\n{esc(prompt)}"))


def on_stop(cfg, payload):
    session = str(first(payload, "session_id", "sessionId") or "ses")
    try:
        os.remove(marker_path(session))
    except OSError:
        pass
    # F17: la Mac liberó la task — si quedaron mensajes encolados (mandados
    # con la task ocupada), despertar la cola YA (auto-resume). Solo para
    # sesiones sin worker del bot (el drain del worker es el dueño de ese
    # relay) y solo la task current (F15: el resto duerme hasta /task).
    try:
        if _queue_peek(session) and WORKERS.get(session) is None \
                and _task_current_ok(cfg, session):
            res = _queue_resume(load_config(), session)
            if res:
                send(cfg, res, loud=False)
    except Exception:  # noqa: BLE001 - el hook jamás rompe la sesión
        pass
    if not cfg.get("notify_stop"):
        return
    if _worker_owned(session) or not _task_current_ok(cfg, session):
        return  # F15: el marker se limpia igual (side-effect ≠ notificación)
    resp = preview(first(payload, "responsePreview", "responseText", "response",
                          "last_assistant_message", "stop_response") or "(sin vista previa)", 500)
    send(cfg, labeled(payload, f"✅ <b>Turno terminado</b>\n{esc(resp)}"))


def _perm_key(sid, name, inp):
    """Huella estable de un pedido de permiso (para dedupe/replay)."""
    try:
        cuerpo = json.dumps(inp, sort_keys=True, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        cuerpo = preview(inp, 500)
    return "%s|%s|%s" % (sid or "?", name,
                         hashlib.sha1(cuerpo.encode("utf-8")).hexdigest()[:16])


def _perm_remember(key, decision):
    """Registra la decisión para replays (el CLI a veces re-dispara el
    mismo PermissionRequest; ver incidente 'apruebo y me lo pide otra
    vez' del 13/10)."""
    def fn(d):
        d = d if isinstance(d, dict) else {}
        d[key] = {"ts": time.time(), "decision": decision}
        # podar: quedarse con las últimas 50
        if len(d) > 50:
            for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:-50]:
                d.pop(k, None)
        return d
    try:
        _store("bot-perm.json").update_cross_process(fn, {})
    except OSError:
        pass


_PERM_REPLAY_S = 600  # misma huella a los <10 min → misma decisión, al toque


def _perm_replay(key):
    d = _store("bot-perm.json").read() or {}
    ent = d.get(key)
    if ent and time.time() - ent.get("ts", 0) < _PERM_REPLAY_S:
        return ent.get("decision")
    return None


def on_permission_request(cfg, payload):
    if not configured(cfg):
        return  # sin setup: gana el diálogo de la app, no esperamos nada
    name, inp = tool_desc(payload)
    if str(name) == "AskUserQuestion":
        # las preguntas las maneja el flujo ❓ (PreToolUse + botones del bot
        # integrado): no duplicar con 🔐 ni competir por el offset de getUpdates
        return
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        # F15: sesión con worker propio (sus permisos headless los reporta el
        # worker) o task no activa — ni mensaje ni espera: gana el diálogo de
        # la app al instante. Antes: tool call retenido hasta 240 s de una
        # sesión que ni se estaba mirando, compitiendo por getUpdates.
        return
    es_plan = str(name) in ("ExitPlanMode", "exit_plan_mode")
    key = _perm_key(sid, str(name), inp)
    # FIX loop-aprobación 13/10: si este pedido EXACTO ya se respondió hace
    # poco, reenviar la misma decisión sin molestar de nuevo — el CLI a
    # veces re-dispara el hook y el usuario terminaba aprobando en loop.
    previa = _perm_replay(key)
    if previa == "allow":
        send(cfg, "👍 Ya aprobado hace un momento — reenviando la decisión.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow"}}})
        return
    if previa == "deny":
        send(cfg, "🚫 Ya rechazado hace un momento — reenviando la decisión.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "deny",
                                                  "message": "Denegado por el usuario desde Telegram"}}})
        return
    if es_plan:
        lines = ["🗺 <b>Plan listo para aprobar</b>"]
        lines.append("\nTocá ✅ para aprobarlo y que empiece la implementación, "
                     "o ✏️ para pedirle ajustes (escribís qué cambiar).")
    else:
        lines = [f"🔐 <b>Permiso requerido</b>: {esc(name)}"]
        if isinstance(inp, dict):
            for k, v in list(inp.items())[:6]:
                lines.append(f"• <b>{esc(str(k))}</b>: {esc(preview(v, 160))}")
        lines.append(f"\nRespondé <b>si</b> / <b>no</b> (o tocá un botón — esperando hasta {cfg.get('wait_timeout_s', 240)} s)")
    kb = [[{"text": "✅ Aprobar", "callback_data": "yes"},
           {"text": "❌ Rechazar", "callback_data": "no"}]]
    if es_plan:
        kb.append([{"text": "✏️ Agregar algo al plan", "callback_data": "planfix"}])
    send(cfg, labeled(payload, "\n".join(lines)),
         markup={"inline_keyboard": kb})
    kind, text = wait_reply(cfg, "permiso pendiente")
    if kind == "yes":
        _perm_remember(key, "allow")
        send(cfg, "👍 Aprobado desde Telegram — el plan queda en ejecución.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow"}}})
        return
    if kind == "no":
        _perm_remember(key, "deny")
        send(cfg, "🚫 Rechazado desde Telegram", loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "deny",
                                                  "message": "Denegado por el usuario desde Telegram"}}})
        return
    if kind == "text" and (text or "").strip() == "planfix":
        # ✏️ ajustes al plan: pedir el texto y rechazar con ese feedback
        send(cfg, "✏️ Escribí qué querés que cambie del plan (cualquier "
                  "comando / cancela y deja el plan pendiente):", loud=False)
        kind2, texto2 = wait_reply(cfg, "ajustes del plan")
        feed = (texto2 or "").strip() if kind2 == "text" else ""
        if not feed or feed.startswith("/"):
            send(cfg, "🙅 Sin ajustes — quedó pendiente en la app.", loud=False)
            return
        _perm_remember(key, "deny")
        send(cfg, "✏️ Ajustes enviados al agente: %s"
             % esc(preview(feed, 200)), loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {
                                         "behavior": "deny",
                                         "message": ("El usuario pide ajustes al plan: %s"
                                                     " — actualizalo y volvé a proponerlo."
                                                     % feed)}}})
        return
    if kind is None:
        send(cfg, "⏱ Sin respuesta por Telegram: quedó el diálogo en la app (si ya respondiste ahí, ignorá esto).")
    # sin decisión: la carrera la gana el diálogo de la app


def on_ask_user_question(cfg, payload):
    _, inp = tool_desc(payload)
    questions = []
    if isinstance(inp, dict):
        q = inp.get("questions")
        if isinstance(q, list):
            questions = q
        elif inp.get("question"):
            questions = [inp]
    if not questions:
        questions = [{"question": preview(inp, 300)}]
    if not configured(cfg):
        return  # la pregunta se muestra normalmente en la app
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: el diálogo queda en la Mac; lo del worker lo reporta él
    # FIX 14/10 (cuestionarios repetidos): el CLI re-dispara el hook y el
    # usuario terminaba eligiendo la opción UNA Y OTRA VEZ. Misma huella
    # respondida hace <10 min → misma respuesta, sin re-mostrar nada.
    key = _perm_key(sid, "AskUserQuestion", inp)
    previa = _perm_replay(key)
    if previa:
        send(cfg, "📨 Ya respondida hace un momento — reenviando: %s"
             % esc(preview(previa, 120)), loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                     "permissionDecision": "deny",
                                     "permissionDecisionReason":
                                     "El usuario respondió por Telegram: %s"
                                     % previa}})
        return
    lines = ["❓ <b>El agente pregunta</b>"]
    for i, q in enumerate(questions, 1):
        lines.append(f"\n<b>{i}. {esc(preview(q.get('question', ''), 200))}</b>")
        for j, opt in enumerate(q.get("options", [])[:4], 1):
            label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""
            lines.append(f"   {j}. {esc(preview(label, 80))}" + (f" — {esc(preview(desc, 100))}" if desc else ""))
    if cfg.get("question_mode", "mac") != "phone":
        # mac: avisar al móvil y devolver ya — el diálogo aparece en la Mac al instante.
        # Desde el teléfono, la vía de respuesta es el bot integrado (entra como prompt).
        lines.append("\n💻 El diálogo ya está abierto en la Mac: respondé ahí."
                     "\n📱 ¿No estás en la Mac? La pregunta llega con botones a @glm_lattenero_bot — respondé ahí.")
        send(cfg, labeled(payload, "\n".join(lines)))
        return
    lines.append(f"\nRespondé con el número (o tocá un botón; esperando {int(cfg.get('question_wait_timeout_s', 60))} s, después también aparece en la Mac)")
    kb = []
    q0 = questions[0] if questions else {}
    for j, opt in enumerate((q0.get("options") or [])[:4], 1):
        label = opt.get("label", "?") if isinstance(opt, dict) else str(opt)
        kb.append([{"text": ("%d. %s" % (j, label))[:36], "callback_data": "ans:%d" % j}])
    send(cfg, labeled(payload, "\n".join(lines)),
         markup={"inline_keyboard": kb} if kb else None)
    qcfg = dict(cfg, wait_timeout_s=int(cfg.get("question_wait_timeout_s", 60)))
    kind, text = wait_reply(qcfg, "pregunta pendiente")
    if kind is None:
        send(cfg, "⏱ Sin respuesta: la pregunta quedó en la app.")
        return  # sin decisión: la pregunta se muestra en la app
    m = re.match(r"^([0-9]+)\b", text or "")
    if m:
        answer = f"opción {m.group(1)}"
    elif kind in ("yes", "no"):
        answer = "sí" if kind == "yes" else "no"
    else:
        answer = text or ""
    send(cfg, f"📨 Respuesta enviada: {esc(preview(answer, 200))}", loud=False)
    _perm_remember(key, answer)  # FIX 14/10: replay si el CLI re-dispara
    # bloqueamos la herramienta; la razón le llega al agente como contexto
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                 "permissionDecision": "deny",
                                 "permissionDecisionReason": f"El usuario respondió por Telegram: {answer}"}})


def on_tool_failure(cfg, payload):
    if not cfg.get("notify_failures"):
        return
    name, _ = tool_desc(payload)
    err = str(first(payload, "error", "tool_error", "stderr") or "")
    for pat in cfg.get("suppress_error_patterns", []):
        try:
            if re.search(pat, err, re.IGNORECASE):
                return  # error auto-recuperable: el agente ya lo está corrigiendo
        except re.error:
            continue
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: fallo de otra task o ya reportado por su worker
    send(cfg, labeled(payload, f"❌ <b>Falló</b> {esc(name)}: {esc(preview(err, 220))}"))


def on_session_start(cfg, payload):
    if not cfg.get("notify_session"):
        return
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: cada turno headless hace resume — eso no es novedad
    source = str(first(payload, "source", "reason", "trigger") or "startup")
    emoji = {"startup": "🟢", "resume": "🔄", "clear": "🧹", "compact": "📦"}.get(source, "🟢")
    send(cfg, labeled(payload, f"{emoji} <b>Sesión</b> ({esc(source)})"))


def run_hook():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"_raw": raw[:2000]}
    cfg = load_config()
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or
                os.environ.get("ZCODE_HOOK_EVENT", "unknown"))
    log_payload(event, payload)
    if event == "PreToolUse":
        name, _ = tool_desc(payload)
        if str(name) == "AskUserQuestion":
            on_ask_user_question(cfg, payload)
        return
    handler = {
        "UserPromptSubmit": on_user_prompt,
        "Stop": on_stop,
        "PermissionRequest": on_permission_request,
        "PostToolUseFailure": on_tool_failure,
        "SessionStart": on_session_start,
    }.get(event)
    if handler:
        handler(cfg, payload)


# ---------------------------------------------------------------- comandos

def cmd_register():
    cfg = load_config()
    if not cfg.get("token"):
        print("Falta el token: zcode-telegram.py set-token <TOKEN>")
        sys.exit(1)
    print("Mandale un mensaje al bot desde Telegram (espero hasta 120 s)…")
    deadline = time.time() + 120
    while time.time() < deadline:
        offset, messages = drain_updates(cfg)
        save_offset(offset)
        if messages:
            msg = messages[-1]
            cfg["chat_id"] = msg["chat"]["id"]
            cfg["owner_user_id"] = msg["from"]["id"]
            cfg["owner_name"] = msg["from"].get("first_name") or msg["from"].get("username") or ""
            save_config(cfg)
            send(cfg, f"✅ Registrado, {cfg['owner_name']}. Los avisos de ZCode llegan acá.")
            print(f"OK chat_id={cfg['chat_id']} user={cfg['owner_name']}")
            return
        time.sleep(2)
    print("No llegó ningún mensaje. Reintentá.")
    sys.exit(1)


def cmd_stuck_watch(token, session):
    cfg = load_config()
    minutes = int(cfg.get("stuck_after_min", 20))
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        time.sleep(30)
        try:
            with open(marker_path(session)) as f:
                current = json.load(f).get("token")
        except (OSError, ValueError):
            return  # el turno terminó (Stop borra el marcador)
        if current != token:
            return  # empezó otro turno
    try:
        with open(marker_path(session)) as f:
            prompt = json.load(f).get("prompt", "")
    except (OSError, ValueError):
        prompt = ""
    if not _task_current_ok(cfg, session):
        return  # F15: la task dejó de ser la current durante la espera
    send(cfg, f"⚠️ <b>Posible sesión trabada</b> hace {minutes} min.\nTurno: {esc(preview(prompt, 200))}")


def cmd_watch_app():
    cfg = load_config()
    state_file = os.path.join(STATE_DIR, "app-running")
    try:
        with open(state_file) as f:
            was = f.read().strip() == "1"
    except OSError:
        was = None
    while True:
        up = subprocess.run(["pgrep", "-x", "ZCode"], capture_output=True).returncode == 0
        if was is not None and up != was:
            send(cfg, "🔴 <b>ZCode se cerró</b>" if not up else "🟢 <b>ZCode volvió a arrancar</b>")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(state_file, "w") as f:
            f.write("1" if up else "0")
        was = up
        time.sleep(60)


def _hq_pending():
    return QUESTIONS.any_pending()


def _reload_now_path():
    return os.path.join(STATE_DIR, "reload-now")


def cmd_watch_commands():
    """Loop del LaunchAgent: atiende comandos (/quota, /ping, /help) del 2_bot.

    No consulta getUpdates mientras hay una espera de permiso/pregunta activa
    (flag tg-busy): un solo lector a la vez para no comerse las respuestas.
    Recarga: si existe state/reload-now y no hay turnos headless en vuelo,
    sale — launchd lo relauna y el proceso nuevo carga el código actualizado.
    """
    menu_ok = False
    housekeeping()  # F24: purga al arrancar (barata, best-effort)
    while True:
        try:
            if os.path.exists(_reload_now_path()) and not _nuclear_active():
                try:
                    os.remove(_reload_now_path())
                except OSError:
                    pass
                log("errors", "reload-now: saliendo para recargar código "
                              "(F16: los turnos en vuelo son procesos "
                              "separados — siguen y se reportan solos)")
                return
            cfg = load_config()
            _orphans_check(cfg)  # F21: huérfanos antes que nada
            if configured(cfg):
                if not menu_ok:
                    menu_ok = register_bot_menu(cfg)
            if configured(cfg):
                outbox_flush(cfg)
            if configured(cfg) and not busy_fresh():
                # F22: long-poll CORTO (3 s) — latencia de comando ≤3 s
                # (antes 10 s de sueño ciego). Corto a propósito: los
                # hooks comparten getUpdates (ADR-001) y un long-poll
                # largo los dejaría sin ventana (409).
                offset = None
                try:
                    offset, messages, _n = fetch_updates(
                        cfg, current_offset(), timeout=3)
                    # FIX 14/10: NO confirmar acá — el offset se confirma
                    # DESPUÉS del dispatch. Confirmar antes dejaba una
                    # ventana donde un kill del poller (ej. el nuclear de
                    # las 02:03) perdía el batch entero: confirmado ante
                    # Telegram, jamás re-entregado, invisible.
                except Exception as exc:
                    log("errors", f"getUpdates falló: {exc}")
                    messages = []
                    time.sleep(3)
                for msg in messages:
                    if not from_owner(cfg, msg):
                        continue
                    if msg.get("chat", {}).get("id") != cfg.get("chat_id"):
                        # F22: mensaje del owner desde OTRO chat (grupo):
                        # responder allá rompería la unidad del chat.
                        # FIX 14/10: con rastro — antes se tragaba en
                        # silencio y parecía "el bot no responde".
                        pwlog("UPDATE de otro chat %s — ignorado (usá el "
                              "chat privado)" %
                              msg.get("chat", {}).get("id"))
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text:
                        if msg.get("photo") or msg.get("voice") \
                                or msg.get("document"):
                            # F23: antes los adjuntos caían en silencio
                            send(cfg, "📎 Vi tu adjunto — por ahora proceso "
                                      "solo texto: pasame la idea en "
                                      "palabras.", loud=False)
                        continue
                    cbd = msg.get("_cb_data") or ""
                    if (cbd.startswith("ans:") or cbd in ("yes", "no")) \
                            and not _hq_pending():
                        # botón de una pregunta que ya no está pendiente:
                        # NO inyectar el texto a la conversación activa
                        send(cfg, "🤔 Ese botón ya venció — no hay pregunta "
                                  "pendiente (/task para la lista).", loud=False)
                        continue
                    pwlog("UPDATE:", text[:60])
                    react(cfg, msg, "👀")  # F23: recibido
                    typing(cfg)  # F23: sin ventana muda
                    try:
                        if not text.startswith("/"):
                            reply = handle_free_text(cfg, text)
                        else:
                            reply = handle_command(cfg, text)
                            if reply is None:
                                reply = "🤔 Comando desconocido — /help"
                    except Exception as exc:  # noqa: BLE001
                        log("errors", "handler: %s" % exc)
                        reply = "⚠️ Error interno procesando tu mensaje: %s" % esc(str(exc)[:120])
                    if reply is not None:
                        if isinstance(reply, tuple):
                            send(cfg, reply[0], loud=False, markup=reply[1])
                        else:
                            send(cfg, reply, loud=False)
                        react(cfg, msg, "✅")  # F23: atendido
                if offset is not None:
                    # FIX 14/10: confirmación POST-dispatch (al-least-once:
                    # un kill a mitad del for re-entrega el batch — perder
                    # mensajes es peor que duplicarlos)
                    save_offset(offset)
        except Exception as exc:  # noqa: BLE001 - el loop no debe morir
            log("errors", f"watch-commands: {exc}")
            try:
                if offset is not None:
                    save_offset(offset)  # batch intentado: no reprocesar eterno
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)  # F22: el wait lo hace el long-poll, no el sueño


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "hook"
    try:
        if cmd == "hook":
            run_hook()
        elif cmd == "set-token" and len(sys.argv) > 2:
            cfg = load_config()
            cfg["token"] = sys.argv[2].strip()
            save_config(cfg)
            print("Token guardado. Ahora: mandale un mensaje al bot y corré 'register'.")
        elif cmd == "register":
            cmd_register()
        elif cmd == "test":
            cfg = load_config()
            ok = send(cfg, "🧪 Test de avisos de ZCode" + (f": {' '.join(sys.argv[2:])}" if len(sys.argv) > 2 else ""))
            print("Enviado" if ok else "Falló (¿token? ¿register?)")
        elif cmd == "quota":
            cfg = load_config()
            report = quota_report()
            print(report)
            send(cfg, report, loud=False)
        elif cmd == "--stuck-watch":
            cmd_stuck_watch(sys.argv[2], sys.argv[3])
        elif cmd == "--run-turn":
            cmd_run_turn()  # F16: entrypoint del worker-actor (proceso)
        elif cmd == "--nuclear-watchdog":
            cmd_nuclear_watchdog()  # Nuclear 2.0: cierre verificado (detached)
        elif cmd == "watch-app":
            cmd_watch_app()
        elif cmd == "watch-commands":
            cmd_watch_commands()
        else:
            print(__doc__)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - jamás romper la sesión por un bug acá
        log("errors", traceback.format_exc())


if __name__ == "__main__":
    main()
