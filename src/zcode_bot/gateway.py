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
    return data[:64]


def answer_callback(cfg, cb):
    """answerCallbackQuery best-effort: corta el spinner del botón tocado."""
    try:
        tg_api(cfg, "answerCallbackQuery", {"callback_query_id": cb.get("id") or ""})
    except Exception as exc:  # noqa: BLE001
        log("errors", f"answerCallbackQuery: {exc}")


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
    set_active_bot_session({"sid": t["sid"], "title": t["title"] or t["sid"][-8:],
                            "ws": t["ws"]})
    activos = running_turns()
    extra = " (⚠️ está corriendo un turno ahora)" if t["sid"] in activos else ""
    return ("🎯 Conversación activa: «%s»%s\nTodo texto que mandes va ahí "
            "(corre headless — sirve con la pantalla bloqueada)." % (esc(t["title"] or t["sid"][-8:]), extra))


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
    """Frena una corrida iniciada DESDE el celular: session/stop con el
    MISMO cliente/runtime del worker que la corre (stop real, sin UI ni
    permisos, pantalla bloqueada OK). /stop solo maneja corridas del bot."""
    got = WORKERS.flag_stop(sid)
    if got is None:
        return ("🤔 Esa corrida ya no existe (terminó o no fue iniciada "
                "desde el celular).")
    c, tit = got
    if not c:
        WORKERS.unflag_stop(sid)
        return "⏳ Esa corrida está arrancando — reintentá /stop en unos segundos."
    r = c.request("session/stop", {"sessionId": sid}, timeout=30)
    if "error" in r:
        WORKERS.unflag_stop(sid)
        return "❌ stop: " + esc(json.dumps(r["error"])[:150])
    return "🛑 Frenando «%s»…" % esc(tit or titulo or sid[-8:])


def stop_list_report():
    """/stop → corridas del celular (Template Method: zcode_bot.reports)."""
    items = WORKERS.items()

    def mins_de(par):
        return max(0, int((time.time() - par[1].get("t0", time.time())) / 60))

    def line_of(par, i):
        sid, w = par
        return "%d. «%s» — %d min\n    %s" % (
            i, esc(w.get("titulo") or sid[-8:]), mins_de(par),
            esc(preview(w.get("prompt") or "", 70)))

    def button_of(par, i):
        sid, w = par
        return {"text": "⏹ %d · %s (%dm)" % (i,
                                              (w.get("titulo") or sid[-8:])[:30],
                                              mins_de(par)),
                "callback_data": "s:%s" % sid}

    return zc_reports.build_list_report(
        items, "🛑 <b>Corridas del celular — tocá para frenar</b>",
        "😌 No hay nada corriendo iniciado desde el celular.\n"
        "Lo que corre en la Mac se frena con el botón Stop de la app.",
        line_of, button_of)


# ---------------------------------------------------------------- nuclear

LAUNCH_LABEL = "com.zcode.telegram-cmds"
NUCLEAR_ACTIVE = [False]  # la puerta reload-now no debe matar un nuclear a mitad


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
    """SIGTERM a los runtimes app-server hijos del poller (los de los
    workers): garantiza el corte aunque algún session/stop haya fallado.
    Los workers lo ven como 'app-server murió' y cierran limpio."""
    run = run or subprocess.run
    try:
        r = run(["pgrep", "-P", str(os.getpid()), "-l"],
                capture_output=True, text=True)
        for line in (r.stdout or "").splitlines():
            partes = line.split()
            if len(partes) >= 2 and partes[1].startswith("zcode-cli"):
                run(["kill", "-TERM", partes[0]], capture_output=True)
    except Exception as exc:  # noqa: BLE001 - nunca frenar el nuclear
        log("errors", "nuclear kill_own_runtimes: %s" % exc)


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


def _nuclear_run(cfg):
    """☢️ Cuerpo de /nuclear: frena TODO (workers del bot + tasks de la UI)
    y reinicia la app y el bot. Corre en un thread del poller."""
    NUCLEAR_ACTIVE[0] = True
    try:
        _nuclear_body(cfg)
    finally:
        NUCLEAR_ACTIVE[0] = False


def _nuclear_body(cfg):
    t0 = time.time()
    pwlog("NUCLEAR inicio")
    send(cfg, "☢️ Nuclear: frenando las corridas del bot…", loud=True)
    frenadas, totales = 0, 0
    for sid, w in list(WORKERS.items()):
        totales += 1
        r = bot_stop(sid, w.get("titulo"))
        pwlog("NUCLEAR stop", sid[-8:], (r or "")[:40])
        if (r or "").startswith("🛑"):
            frenadas += 1
    _nuc_kill_own_runtimes()
    send(cfg, "☢️ Cerrando la app ZCode de la Mac (todo lo de la UI se frena ahí)…",
         loud=True)
    rep = app_restart_cycle()
    dur = int(time.time() - t0)
    notes = (" · " + "; ".join(rep["notes"])) if rep["notes"] else ""
    if rep["app_ok"]:
        final = ("☢️ <b>Nuclear completo</b> (%ds):\n"
                 "⏹️ %d/%d corridas del bot frenadas\n"
                 "🖥️ App ZCode cerrada y reabierta%s\n"
                 "🔁 Bot reiniciándose — /ping para probarlo"
                 % (dur, frenadas, totales, esc(notes)))
    else:
        final = ("☢️ <b>Nuclear con problemas</b> (%ds):\n"
                 "⏹️ %d/%d corridas del bot frenadas\n"
                 "⚠️ La app no volvió a arrancar%s — abrila a mano."
                 % (dur, frenadas, totales, esc(notes)))
    send(cfg, final, loud=True)
    pwlog("NUCLEAR fin:", final.replace("\n", " ")[:120])
    # reinicio del bot: el offset quedó committeado ANTES del dispatch del
    # comando (save_offset precede al handler) → el /nuclear no se reprocesa
    time.sleep(2)
    try:
        subprocess.run(["launchctl", "kickstart", "-k",
                        "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)],
                       capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log("errors", "nuclear kickstart: %s" % exc)


def _cmd_nuclear(cfg, rest):
    """/nuclear — EMERGENCIA: frena TODO (corridas del celular + tasks de la
    UI de la Mac) y reinicia la app y el bot. No hay entrada externa al
    runtime de la app (ADR-006): cerrar y reabrir ES el mecanismo. Solo se
    ejecuta con 'go' — que solo produce el botón de confirmación."""
    r = (rest or "").strip().lower()
    if r == "go":
        import threading as _th
        _th.Thread(target=_nuclear_run, args=(cfg,), daemon=True).start()
        return "☢️ Ejecutando — frena todo y reinicia. Te aviso al terminar."
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


def _turn_end_poll(sid, t0, timeout=1800):
    """Espera turn.completed/failed de sid (ts >= t0). Lee solo la COLA del
    último log (512KB) — el f.read() entero sumaba decenas de MB a la RSS."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fin = None
        for _ts, _s, _ev, d in zc_logs.iter_events(
                BASE, needles=("turn.completed", "turn.failed"),
                sids={sid}, since_epoch=t0, last_files=1,
                tail_bytes=512 * 1024):
            fin = d
        if fin is not None:
            return fin
        time.sleep(4)
    return None


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
COT_MAX_MSGS = 30  # tope de mensajes de CoT por turno (~90K chars en chat)
EVENTS_TRIP = 1200  # replay mayor a esto → live OFF (anti-OOM, ver _live_view)
LIVE_FRAME_TRIP = 25 * 1024 * 1024  # un frame de session/events mayor a 25MB
# significa replay con imágenes (caso real: 3.2GB de pico de RSS en UNA
# lectura con apenas 329 eventos) → live OFF + flag por sesión
LIVE_SKIP_TTL = 6 * 3600  # el flag vive 6 h; el compact achica el replay y
# lo limpia (ver zcode_compact), tras lo cual el live vuelve solo


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
    "kill/pkill/killall/launchctl kickstart sobre zcode, app-server o el "
    "agente com.zcode.telegram-cmds matan a TU PROPIO huésped y este turno "
    "muere con «Turn was cancelled» (incidente real 2026-10-13 15:12). Para "
    "reiniciar el bot, pedile /nuclear al usuario por el chat.)"
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
                        ch=COT_CHUNK, allowance=COT_MAX_MSGS):
    """Trozos listos para mandar como mensajes PERMANENTES de CoT: todo lo
    acumulado que excede la cola viva (tail_keep), en piezas de ch. La cola
    sigue editándose aparte — así el CoT queda scrolleable en el chat
    mientras corre el turno (límite 4096/mensaje = el porqué de los trozos)."""
    out = []
    while len(cot_full) - flushed > tail_keep and len(out) < allowance:
        trozo = cot_full[flushed:flushed + ch]
        out.append(trozo)
        flushed += len(trozo)
    return out, flushed


def _steps_of_messages(msgs):
    """[(nombre, desc)] de los tool calls de una lista de mensajes (parts
    type=='tool': state.input trae command/file_path). Tolerante al shape."""
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        if not isinstance(m, dict):
            continue
        for p in m.get("parts") or []:
            if not (isinstance(p, dict) and p.get("type") == "tool"):
                continue
            st = p.get("state") or {}
            nombre = p.get("tool") or p.get("name") or st.get("name") or "tool"
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            desc = ""
            if isinstance(inp.get("command"), list):
                desc = " ".join(str(x) for x in inp["command"])[:90]
            elif isinstance(inp.get("command"), str):
                desc = inp["command"][:90]
            elif inp.get("file_path"):
                desc = str(inp["file_path"])
            elif inp.get("path"):
                desc = str(inp["path"])
            elif inp.get("pattern"):
                desc = "pattern " + str(inp["pattern"])[:60]
            out.append((str(nombre), desc))
    return out


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
    """Al terminar un turno: persiste el trace y lo manda como documento
    (pasos + CoT completo + respuesta). Config trace_documents=false apaga
    el documento; el archivo state/last-turn-trace.txt queda siempre."""
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
    if not cfg.get("trace_documents", True):
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


WORKERS = zc_conc.WorkersRegistry()   # sid -> {t0, prompt, ws, titulo, client, stopped}
QUESTIONS = zc_conc.QuestionsBroker()  # requestId -> PendingQuestion


def pwlog(*a):
    """Log de pasos del bot unificado (worker + entradas del poller)."""
    import datetime
    try:
        with open(os.path.join(BASE, "hooks", "logs", "prompt-worker.log"), "a") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"),
                                    " ".join(str(x) for x in a)))
    except OSError:
        pass


def _rss_mb():
    """RSS máximo del proceso en MB (ru_maxrss viene en bytes en macOS)."""
    import resource
    try:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576)
    except Exception:
        return -1


def _user_input_handler(cfg, titulo):
    """Callback de interaction/requestUserInput: manda la ❓ al chat y
    bloquea hasta la respuesta (o timeout -> decline)."""
    def handler(params):
        qs = params.get("questions") or []
        rid = str(params.get("requestId") or time.time())
        pend = QUESTIONS.ask(rid, qs, titulo)
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
        pend.event.wait(timeout=480)
        QUESTIONS.pop(rid)
        if pend.event.is_set():
            return {"action": "accept",
                    "content": {"answer": pend.answer}}
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
        _store("bot-queue.json").update(add, {})
    except OSError:
        pass


def _queue_take(sid):
    taken = [[]]

    def take(q):
        q = q if isinstance(q, dict) else {}
        taken[0] = q.pop(sid, [])
        return q
    try:
        _store("bot-queue.json").update(take, {})
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
    razon = ""
    texto = ""
    n_deltas = 0
    vistos = set()
    pasos = []
    ph_pasos = None
    pasos_last = last_id
    last_edit = 0.0
    last_poll_pasos = 0.0
    cot_full = ""    # CoT pendiente de mandar (lo enviado al chat se LIBERA)
    cot_flushed = 0  # chars ya enviados como mensajes permanentes
    cot_nmsg = 0
    cot_cortado = False
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
        el ~1 mensaje/s por chat de la FAQ de Telegram."""
        nonlocal cot_flushed, cot_nmsg, cot_cortado, live_id, live_moved
        nonlocal cot_full  # sin esto, la asignación de liberación la vuelve
        # LOCAL y la primera lectura (línea _cot_chunks_to_send) explota con
        # UnboundLocalError — turno muerto (incidente 14:28 "Probando cot 1234")
        if cot_cortado or not cfg.get("cot_stream", True):
            return
        trozos, nf = _cot_chunks_to_send(
            cot_full, cot_flushed, tail_keep=tail_keep,
            allowance=COT_MAX_MSGS - cot_nmsg)
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
        if cot_nmsg >= COT_MAX_MSGS and len(cot_full) - cot_flushed > 0:
            cot_cortado = True
            send(cfg, "🧚 …el CoT sigue, pero corto el chat acá (%d mensajes) "
                      "— el documento final lo trae COMPLETO." % COT_MAX_MSGS,
                 loud=False)

    deadline = time.time() + 2100
    while time.time() < deadline:
        if not th.is_alive():
            break
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
                    pwlog("live evs=%d frame=%dMB rss=%dMB"
                          % (len(evs), c.max_frame // 1048576, _rss_mb()))
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
                            razon += p["delta"]
                            cot_full += p["delta"]
                            n_deltas += 1
                        elif k == "text_delta" and p.get("delta"):
                            texto += p["delta"]
                            n_deltas += 1
                            if razon and not evento_write:
                                # EVENTO DE FASE: pasó de pensar a escribir —
                                # mensaje propio, abajo de las cajas de CoT
                                evento_write = True
                                if cfg.get("cot_stream", True):
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
            except Exception:  # noqa: BLE001
                pass
        if live_id and time.time() - last_edit >= 3.0:
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
        time.sleep(2.0)
    # fin del turno: el CoT restante también a mensajes permanentes —
    # el chat queda con el razonamiento COMPLETO scrolleable
    try:
        cot_flush(0, pace=False)
    except Exception:  # noqa: BLE001
        pass
    pwlog("live fin: deltas=%d razon=%d texto=%d pasos=%d cot_msgs=%d"
          % (n_deltas, len(razon), len(texto), len(pasos), cot_nmsg))
    return cot_full, texto, pasos, live_id


def prompt_worker(cfg, act, text):
    """Corre un prompt headless en la conversación activa y empuja la
    respuesta a Telegram. Vive en un thread del poller."""
    import threading as _th
    sid, ws, titulo = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    key = sid or "nueva"
    if sid and WORKERS.get(sid) is not None:
        # ya hay un worker para la sesión (carrera poller/queue_drain):
        # encolar sin avisar — el que corre hace drain al terminar
        _queue_put(sid, act, text)
        return
    WORKERS.register(key, {"t0": time.time(), "prompt": text[:80],
                           "ws": ws, "titulo": titulo, "client": None})
    pwlog("worker start sid=%s titulo=%s rss=%dMB texto=%r"
          % (sid, titulo, _rss_mb(), text[:60]))
    try:
        c = zc_proto.make(cwd=ws,
                          on_user_input=_user_input_handler(cfg, titulo),
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
    # el cliente queda registrado: /stop frenaría la corrida con ÉL (mismo
    # runtime = stop real; otro runtime no ve turnos ajenos)
    WORKERS.set_client(key, c)
    snap_sel = None  # /stop puede frenar ANTES de la asignación (finally la lee)
    try:
        if sid in running_turns():
            pwlog("busy -> encolar")
            _queue_put(sid, act, text)
            send(cfg, "⏳ «%s» está corriendo — tu mensaje quedó encolado "
                      "(corre al terminar)." % esc(titulo))
            return
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
                fin=_turn_end_poll(sid, t0, timeout=1800)), daemon=True)
            th.start()
            lr = _live_view(c, cfg, sid, t0, titulo, ph, th, last_id=last_id)
            live_cot = (lr[0] if lr else "") or ""
            live_box = (lr[3] if lr else None) or ph  # caja viva (abajo si hubo CoT)
            fin = res.get("fin")
            if fin is None:  # la vista cortó por deadline: esperar el resto
                fin = _turn_end_poll(sid, t0, timeout=600)
        else:
            fin = _turn_end_poll(sid, t0, timeout=1800)
            live_cot = ""
            live_box = ph
        pwlog("fin:", (fin or {}).get("event"), "rss=%dMB" % _rss_mb())
        if ph:
            if fin is None:
                _live_edit(cfg, live_box, "⏱️ <b>«%s»</b> sigue corriendo — /status"
                           % esc(titulo))
            elif fin.get("event") == "turn.failed":
                _live_edit(cfg, live_box, "❌ <b>«%s»</b> falló — detalle abajo"
                           % esc(titulo))
            else:
                _live_edit(cfg, live_box, "✅ <b>«%s»</b> · respuesta:" % esc(titulo))
        if fin is None:
            send(cfg, "⏱️ «%s» sigue corriendo tras 30 min — sigue de fondo; /status." % esc(titulo))
            return
        if fin.get("event") == "turn.failed":
            err = str((fin.get("error") or {}).get("message", ""))[:150]
            send(cfg, "❌ «%s» falló: %s" % (esc(titulo), esc(err)))
            return
        if WORKERS.stopped(sid):
            if ph:
                _live_edit(cfg, live_box, "⏹️ <b>«%s»</b> frenado" % esc(titulo))
            send(cfg, "⏹️ «%s» frenado por /stop." % esc(titulo))
            return
        dur = (time.time() - t0) / 60.0
        msgs = _fetch_messages(c, sid, after_id=last_id, limit=120) or []
        texto = _extract_assistant_text(msgs).strip()
        _turn_trace(cfg, titulo, sid, dur, msgs, live_cot=live_cot)
        if not texto:
            send(cfg, "✅ «%s» listo en %.0f min (sin texto de respuesta)." % (esc(titulo), dur))
            return
        _send_long_reply(cfg, titulo, dur, texto)
    except Exception as e:
        # un worker muerto NUNCA más es silencioso: avisar al chat + log
        import traceback
        pwlog("worker CRASH:", traceback.format_exc()[-500:])
        try:
            send(cfg, "⚠️ El worker de «%s» murió procesando tu mensaje "
                  "(reenvialo cuando quieras): <code>%s</code>" %
                  (esc(titulo), esc(str(e)[:150])), loud=True)
        except Exception:
            pass
    finally:
        WORKERS.pop(sid or "nueva")
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
    lanza el siguiente (cadena)."""
    import threading as _th
    if not sid:
        return
    lst = _queue_take(sid)
    if lst:
        item = lst[0]
        for resto in lst[1:]:  # los devolvemos a la cola
            _queue_put(sid, resto["act"], resto["text"])
        _th.Thread(target=prompt_worker, args=(cfg, item["act"], item["text"]),
                   daemon=True).start()


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
        ("status", "qué está corriendo + conversación activa"),
        ("ctx", "contexto usado por cada task"),
        ("compact", "elegir task para compactar (Projects)"),
        ("all_compact", "ídem, incluidas las sueltas del grupo Tasks"),
        ("run", "nueva tarea y mandar prompt (/run proyecto prompt)"),
        ("stop", "frenar lo que corrés desde el celular (botones)"),
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
    # 1) respuesta a una pregunta del agente headless
    pend = QUESTIONS.first_pending()
    if pend is not None:
        resp = text.strip()
        qs = pend.questions or []
        # numero -> label/value de la opcion
        if resp.isdigit() and qs:
            try:
                ops = (qs[0].get("options") or [])
                op = ops[int(resp) - 1]
                resp = op.get("value") or op.get("label") or resp
            except (IndexError, ValueError):
                pass
        QUESTIONS.answer(pend.request_id, resp)
        return "✅ Respondido: %s" % esc(resp[:200])
    act = active_bot_session()
    if not act:
        return ("💬 Todavía no hay conversación activa — mandá /task y elegí "
                "una con /task n.\nDespués, todo texto que mandes corre ahí "
                "(headless, pantalla bloqueada OK).")
    _th.Thread(target=prompt_worker, args=(cfg, act, text), daemon=True).start()
    return "▶️ «%s» — trabajando (headless)…" % esc(act.get("title", "?"))


def status_report():
    act = active_bot_session()
    lines = ["📊 <b>Estado</b>"]
    if act:
        tag = os.path.basename(act.get("ws") or "")
        lines.append("🎯 Activa: «%s»%s" % (esc(act.get("title", "")),
                                           " · 📁" + esc(tag) if tag else ""))
    else:
        lines.append("🎯 Activa: ninguna (/task n para elegir)")
    activos = running_turns()
    if activos:
        for sid, ts in activos.items():
            titulo = session_title_from_db(sid) or sid[-8:]
            import datetime
            lines.append("🔄 «%s» desde %s" % (esc(titulo),
                          datetime.datetime.fromtimestamp(ts).strftime("%H:%M")))
    else:
        lines.append("😌 Nada corriendo.")
    return "\n".join(lines)


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
    _th.Thread(target=prompt_worker, args=(cfg, act, prompt), daemon=True).start()
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


def _cmd_mode(cfg, rest):
    act = active_bot_session()
    if not act:
        return "⚠️ Elegí conversación primero."
    modos = ("yolo", "build", "edit", "plan", "guarded")
    if not rest:
        return ("🎛 Modo: %s\n/mode yolo · build · edit · plan · guarded"
                % esc(act.get("mode") or "yolo"))
    if rest not in modos:
        return "⚠️ Modos: " + " · ".join(modos)
    set_active_bot_session(dict(act, mode=rest))
    return "🎛 Modo de la conversación activa: %s (aplica en el próximo turno)" % esc(rest)


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


def _cmd_help(cfg, rest):
    return ("🤖 <b>Bot unificado ZCode</b> (headless — sirve con la Mac bloqueada)\n"
            "<b>Conversaciones</b>\n"
            "/task — con Project · /task n elige · /task new [proyecto]\n"
            "/all_tasks — todas, incluidas las sueltas del grupo Tasks\n"
            "texto libre — corre en la conversación activa\n"
            "/run [proyecto] prompt — nueva conversación + prompt\n"
            "/model — modelo de la activa · /thinking — max/high/low\n"
            "/mode — yolo|build|edit|plan|guarded\n"
            "/fork — bifurcar la activa · /last — última respuesta completa\n"
            "/cot — paso a paso + CoT completo del turno (documento)\n"
            "<b>Control</b>\n"
            "/quota — credits 5h + semanal del plan\n"
            "/usage — detalle de hoy por sesión\n"
            "/ctx — tasks con % de contexto\n"
            "/ping — prueba de vida\n"
            "/status — qué está corriendo + conversación activa\n"
            "/stop — frená tus corridas del celular (solo esas, botones)\n"
            "/nuclear — ☢️ emergencia: frena TODO (celular + Mac) y reinicia\n"
            "/compact — lista Projects para compactar · /all_compact todas\n"
            "<b>Info</b>\n"
            "/ping — prueba de vida\n"
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
    (("task",), _cmd_task),
    (("all_tasks", "alltasks", "tasks"), _cmd_all_tasks),
    (("status",), _cmd_status),
    (("last",), _cmd_last),
    (("cot", "trace"), _cmd_cot),
    (("run",), _cmd_run),
    (("model",), _cmd_model),
    (("thinking",), _cmd_thinking),
    (("mode",), _cmd_mode),
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
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "ses")
    return os.path.join(STATE_DIR, f"turn-{safe}.marker")


def on_user_prompt(cfg, payload):
    prompt = preview(first(payload, "prompt", "text", "message") or "", 200)
    session = str(first(payload, "session_id", "sessionId") or "ses")
    token = f"{time.time_ns()}"
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(marker_path(session), "w") as f:
        f.write(json.dumps({"token": token, "prompt": prompt}))
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")  # shim: entrypoint de subprocesos
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    subprocess.Popen(
        [sys.executable, me, "--stuck-watch", token, session],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if cfg.get("notify_prompt"):
        send(cfg, labeled(payload, f"▶️ <b>Turno iniciado</b>\n{esc(prompt)}"))


def on_stop(cfg, payload):
    session = str(first(payload, "session_id", "sessionId") or "ses")
    try:
        os.remove(marker_path(session))
    except OSError:
        pass
    if not cfg.get("notify_stop"):
        return
    resp = preview(first(payload, "responsePreview", "responseText", "response",
                          "last_assistant_message", "stop_response") or "(sin vista previa)", 500)
    send(cfg, labeled(payload, f"✅ <b>Turno terminado</b>\n{esc(resp)}"))


def on_permission_request(cfg, payload):
    if not configured(cfg):
        return  # sin setup: gana el diálogo de la app, no esperamos nada
    name, inp = tool_desc(payload)
    if str(name) == "AskUserQuestion":
        # las preguntas las maneja el flujo ❓ (PreToolUse + botones del bot
        # integrado): no duplicar con 🔐 ni competir por el offset de getUpdates
        return
    lines = [f"🔐 <b>Permiso requerido</b>: {esc(name)}"]
    if isinstance(inp, dict):
        for k, v in list(inp.items())[:6]:
            lines.append(f"• <b>{esc(str(k))}</b>: {esc(preview(v, 160))}")
    lines.append(f"\nRespondé <b>si</b> / <b>no</b> (o tocá un botón — esperando hasta {cfg.get('wait_timeout_s', 240)} s)")
    send(cfg, labeled(payload, "\n".join(lines)),
         markup={"inline_keyboard": [[{"text": "✅ Sí", "callback_data": "yes"},
                                      {"text": "❌ No", "callback_data": "no"}]]})
    kind, text = wait_reply(cfg, "permiso pendiente")
    if kind == "yes":
        send(cfg, "👍 Aprobado desde Telegram", loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow"}}})
    if kind == "no":
        send(cfg, "🚫 Denegado desde Telegram", loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "deny",
                                                  "message": "Denegado por el usuario desde Telegram"}}})
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
    send(cfg, labeled(payload, f"❌ <b>Falló</b> {esc(name)}: {esc(preview(err, 220))}"))


def on_session_start(cfg, payload):
    if not cfg.get("notify_session"):
        return
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
    while True:
        try:
            if os.path.exists(_reload_now_path()) and WORKERS.empty() \
                    and not NUCLEAR_ACTIVE[0]:
                try:
                    os.remove(_reload_now_path())
                except OSError:
                    pass
                log("errors", "reload-now: saliendo para recargar código "
                              "(sin turnos headless en vuelo)")
                return
            cfg = load_config()
            if configured(cfg):
                if not menu_ok:
                    menu_ok = register_bot_menu(cfg)
            if configured(cfg):
                outbox_flush(cfg)
            if configured(cfg) and not busy_fresh():
                offset, messages = drain_updates(cfg)
                save_offset(offset)
                for msg in messages:
                    if not from_owner(cfg, msg):
                        continue
                    text = (msg.get("text") or "").strip()
                    cbd = msg.get("_cb_data") or ""
                    if (cbd.startswith("ans:") or cbd in ("yes", "no")) \
                            and not _hq_pending():
                        # botón de una pregunta que ya no está pendiente:
                        # NO inyectar el texto a la conversación activa
                        send(cfg, "🤔 Ese botón ya venció — no hay pregunta "
                                  "pendiente (/task para la lista).", loud=False)
                        continue
                    pwlog("UPDATE:", text[:60])
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
        except Exception as exc:  # noqa: BLE001 - el loop no debe morir
            log("errors", f"watch-commands: {exc}")
        time.sleep(10)


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
