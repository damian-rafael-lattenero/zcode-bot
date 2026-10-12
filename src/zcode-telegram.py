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


def _outbox_path():
    return os.path.join(BASE, "hooks", "state", "outbox.json")


def _outbox_put(text):
    try:
        try:
            with open(_outbox_path()) as f:
                box = json.load(f)
        except (OSError, ValueError):
            box = []
        box.append({"ts": int(time.time()), "text": text[:3900]})
        with open(_outbox_path(), "w") as f:
            json.dump(box[-20:], f, ensure_ascii=False)
    except OSError:
        pass


def outbox_flush(cfg):
    """Reenvía lo acumulado cuando la red volvió. Devuelve cantidad enviada."""
    try:
        with open(_outbox_path()) as f:
            box = json.load(f)
    except (OSError, ValueError):
        return 0
    if not box:
        return 0
    restan = []
    ok = 0
    for item in box:
        if send(cfg, item["text"], loud=False):
            ok += 1
        else:
            restan.append(item)
    try:
        with open(_outbox_path(), "w") as f:
            json.dump(restan, f, ensure_ascii=False)
    except OSError:
        pass
    return ok


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


def busy_fresh(max_age=120):
    """True si hay una espera de permiso/pregunta activa (el poller no debe tocar getUpdates)."""
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
    lateral de la app (pinned primero, luego updated_at desc). Cada una
    con su rank (posición dentro del sidebar de SU workspace) para poder
    clickearla. Devuelve lista de dicts; los números n son 1-based.
    """
    import sqlite3
    dbp = os.path.join(BASE, "cli", "db", "db.sqlite")
    idxp = os.path.join(BASE, "v2", "tasks-index.sqlite")
    if not (os.path.exists(dbp) and os.path.exists(idxp)):
        return []
    db = sqlite3.connect(dbp)
    usage = {}
    for sid, inp, out, model, ms in db.execute(
        "SELECT m.session_id, m.input_tokens, m.output_tokens, m.model_id, m.started_at "
        "FROM model_usage m JOIN (SELECT session_id, MAX(started_at) ms FROM model_usage "
        "GROUP BY session_id) l ON l.session_id=m.session_id AND l.ms=m.started_at "
        "WHERE m.session_id NOT LIKE 'sess_subagent%'"
    ):
        usage[sid] = {"ctx": (inp or 0) + (out or 0), "model": model, "when": ms}
    db.close()
    idx = sqlite3.connect(idxp)
    # OJO: tasks-index guarda filas legacy por task (workspace_key = hash
    # viejo) junto a las nuevas (workspace_key = path); el sidebar de la app
    # muestra solo las nuevas → deduplicar por task_id prefiriendo esas.
    rows = idx.execute(
        "SELECT task_id, title, workspace_path, updated_at, "
        "(workspace_key = workspace_path) AS es_nueva "
        "FROM tasks WHERE deleted=0 AND archived=0 "
        "ORDER BY es_nueva DESC, pinned DESC, updated_at DESC"
    ).fetchall()
    idx.close()
    rank_ws = {}
    tasks = []
    vistos = set()
    for tid, title, ws, _ua, _nueva in rows:
        if tid in vistos:
            continue  # fila legacy del mismo task (workspace_key hash viejo)
        vistos.add(tid)
        if tid not in usage:
            continue  # sin consumo → sin contexto que compactar
        rank = rank_ws.get(ws, 0)
        rank_ws[ws] = rank + 1
        u = usage[tid]
        tasks.append({"n": len(tasks) + 1, "sid": tid, "title": title or "",
                      "ws": ws, "rank": rank, "ctx": u["ctx"],
                      "model": u["model"], "when": u["when"]})
        if len(tasks) >= limit:
            break
    return tasks


def _compactiones(horas=48):
    """{sessionId: epoch} del último compact.completed por sesión, según el
    log del CLI. El % de contexto no baja hasta el próximo turno — esta
    marca es la prueba visible de que el compact corrió."""
    import datetime
    import glob as globmod
    out = {}
    limite = time.time() - horas * 3600
    files = sorted(globmod.glob(os.path.join(
        BASE, "cli", "log", "zcode-*.jsonl")))[-2:]
    for f in files:
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"compact.completed"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                sid = d.get("sessionId") or ""
                if not sid:
                    continue
                try:
                    ts = datetime.datetime.fromisoformat(
                        d["timestamp"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    continue
                if ts >= limite:
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
    """Título de una sesión desde el db del CLI (para confirmar cuál compactó)."""
    import sqlite3
    try:
        db = sqlite3.connect(os.path.join(BASE, "cli", "db", "db.sqlite"))
        row = db.execute("SELECT title FROM session WHERE id=?", (sid,)).fetchone()
        db.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def compact_list_report(scope="projects"):
    """/compact → lista Projects para elegir cuál compactar (botones c:sid);
    scope 'all' (/all_compact) incluye las sueltas del grupo Tasks."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    if not tasks:
        return ("🧠 Sin tasks%s para compactar — /all_compact muestra todas."
                % ("" if scope == "all" else " de proyecto"))
    activos = running_turns()
    act = active_bot_session()
    lines = ["🧹 <b>Compactar — tocá una</b> (solo idle)" +
             (" · /all_compact para todas" if scope == "all" else "")]
    filas = []
    for t in tasks:
        marca = "🔄" if t["sid"] in activos else ("🎯" if act and t["sid"] == act.get("sid") else "·")
        titulo = t["title"] or t["sid"][-8:]
        lines.append("%s %d. %s — %s" % (marca, t["n"], esc(titulo), fmt_tok(t["ctx"])))
        filas.append([{"text": "%s %d · %s (%s)" % (marca, t["n"], titulo[:28], fmt_tok(t["ctx"])),
                       "callback_data": "c:%s" % t["sid"]}])
    lines.append("\n🔄 no se puede (está corriendo) · 🎯 es la activa.")
    return "\n".join(lines), {"inline_keyboard": filas}


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
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import zcode_proto as zp
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
        c = zp.ProtoClient(
            cwd=ws,
            stderr_path=os.path.join(BASE, "hooks", "logs", "proto-stderr.log"))
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
    """Sesiones con un turno corriendo ahora, según el log del CLI."""
    import datetime
    import glob as globmod
    active = {}
    files = sorted(globmod.glob(os.path.join(BASE, "cli", "log", "zcode-*.jsonl")))
    for f in files[-2:]:
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"turn.' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ev = d.get("event")
                if ev not in ("turn.started", "turn.completed", "turn.failed"):
                    continue
                sid = d.get("sessionId") or ""
                if not sid or sid.startswith("sess_subagent"):
                    continue
                try:
                    ts = datetime.datetime.fromisoformat(
                        d["timestamp"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    continue
                if ev == "turn.started":
                    if ts >= time.time() - max_age:
                        active[sid] = ts
                else:
                    active.pop(sid, None)
    return active


# ---------------------------------------------------------------- U1: bot unificado

MODEL_SEL = {"providerId": "zai-bot", "modelId": "GLM-5.3",
             "options": {"reasoningLevel": "high"}}
THINKING_LEVELS = ("max", "high", "low")
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
    try:
        with open(_bot_session_state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def set_active_bot_session(d):
    try:
        with open(_bot_session_state_path(), "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


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
    """Lista numerada de conversaciones para /task (scope 'projects') o
    /all_tasks (scope 'all'). Devuelve (texto, markup con botones)."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    if not tasks:
        return ("🧠 Sin tasks%s — /all_tasks muestra todas."
                % ("" if scope == "all" else " de proyecto"))
    activos = running_turns()
    act = active_bot_session()
    if scope == "all":
        lines = ["💬 <b>Todas las conversaciones</b> (incluye sueltas) — /task solo proyectos"]
    else:
        lines = ["💬 <b>Conversaciones por proyecto</b> — tocá una (o /task n)"]
    filas = []
    for t in tasks:
        marca = "🔄" if t["sid"] in activos else ("🎯" if act and t["sid"] == act.get("sid") else "·")
        tag = "" if t["ws"] == active_workspace_path() else " · 📁" + esc(os.path.basename(t["ws"] or ""))
        titulo = t["title"] or t["sid"][-8:]
        lines.append("%s %d. %s%s" % (marca, t["n"], esc(titulo), tag))
        filas.append([{"text": "%s %d · %s" % (marca, t["n"], titulo[:34]),
                       "callback_data": "t:%s" % t["sid"]}])
    lines.append("\nTexto libre que mandes va a la conversación activa (🎯).")
    return "\n".join(lines), {"inline_keyboard": filas}


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
    """Espera en el log del CLI el evento de `sid` con ts >= t0 (para
    compact.*/turn.*). Devuelve el dict del evento o None."""
    import datetime
    logf = os.path.join(BASE, "cli", "log",
                        "zcode-%s.jsonl" % datetime.date.today().strftime("%Y-%m-%d"))
    deadline = time.time() + timeout
    needles = tuple('"%s"' % e for e in eventos)
    while time.time() < deadline:
        try:
            with open(logf, errors="replace") as f:
                data = f.read()
        except OSError:
            data = ""
        for needle in needles:
            i = data.rfind(needle)
            if i < 0:
                continue
            lo = data.rfind("\n", 0, i) + 1
            hi = data.find("\n", i)
            try:
                d = json.loads(data[lo:hi if hi > 0 else len(data)])
            except ValueError:
                continue
            ts = datetime.datetime.fromisoformat(
                d["timestamp"].replace("Z", "+00:00")).timestamp()
            if ts >= t0 - 1 and d.get("sessionId") == sid:
                return d
        time.sleep(3)
    return None


def _model_snapshot(sid):
    """Última runtime/model_selection de la sesión (data JSON) o None."""
    import sqlite3
    try:
        db = sqlite3.connect(os.path.join(BASE, "cli", "db", "db.sqlite"))
        row = db.execute("SELECT data FROM session_entry WHERE session_id=? "
                         "AND type='runtime/model_selection' "
                         "ORDER BY time_created DESC LIMIT 1", (sid,)).fetchone()
        db.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


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
    except sqlite3.Error:
        pass


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
    w = HEADLESS_WORKERS.get(sid)
    if not w:
        return ("🤔 Esa corrida ya no existe (terminó o no fue iniciada "
                "desde el celular).")
    c = w.get("client")
    if not c:
        return "⏳ Esa corrida está arrancando — reintentá /stop en unos segundos."
    w["stopped"] = True
    r = c.request("session/stop", {"sessionId": sid}, timeout=30)
    if "error" in r:
        w["stopped"] = False
        return "❌ stop: " + esc(json.dumps(r["error"])[:150])
    return "🛑 Frenando «%s»…" % esc(w.get("titulo") or titulo or sid[-8:])


def stop_list_report():
    """/stop → SOLO las corridas iniciadas desde el celular, tocables."""
    if not HEADLESS_WORKERS:
        return ("😌 No hay nada corriendo iniciado desde el celular.\n"
                "Lo que corre en la Mac se frena con el botón Stop de la app.")
    lines = ["🛑 <b>Corridas del celular — tocá para frenar</b>"]
    filas = []
    for i, (sid, w) in enumerate(list(HEADLESS_WORKERS.items()), 1):
        mins = max(0, int((time.time() - w.get("t0", time.time())) / 60))
        titulo = w.get("titulo") or sid[-8:]
        lines.append("%d. «%s» — %d min\n    %s"
                     % (i, esc(titulo), mins, esc(preview(w.get("prompt") or "", 70))))
        filas.append([{"text": "⏹ %d · %s (%dm)" % (i, titulo[:30], mins),
                       "callback_data": "s:%s" % sid}])
    return "\n".join(lines), {"inline_keyboard": filas}


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
    """Espera turn.completed/failed de sid en el log del CLI (ts >= t0).
    Lee solo la COLA del archivo (binario, 512KB): el f.read() entero
    slurpeaba el log del día cada 4s y sumaba decenas de MB a la RSS."""
    import datetime
    deadline = time.time() + timeout
    TAIL = 512 * 1024
    while time.time() < deadline:
        logf = os.path.join(BASE, "cli", "log",
                            "zcode-%s.jsonl" % datetime.date.today().strftime("%Y-%m-%d"))
        try:
            with open(logf, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - TAIL))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            data = ""
        for evn in ('"turn.completed"', '"turn.failed"'):
            i = data.rfind(evn)
            while i >= 0:
                lo = data.rfind("\n", 0, i) + 1
                hi = data.find("\n", i)
                try:
                    d = json.loads(data[lo:hi if hi > 0 else len(data)])
                except ValueError:
                    d = None
                if d:
                    try:
                        ts = datetime.datetime.fromisoformat(
                            d["timestamp"].replace("Z", "+00:00")).timestamp()
                    except (KeyError, ValueError):
                        ts = 0
                    if ts >= t0 - 1 and d.get("sessionId") == sid:
                        return d
                i = data.rfind(evn, 0, i)
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


HEADLESS_WORKERS = {}  # sid -> {"t0": epoch, "prompt": str}
_WORKERS_LOCK = __import__("threading").Lock()
HEADLESS_QUESTIONS = {}  # requestId -> {"event", "answer", "qs", "titulo"}


def pwlog(*a):
    """Log de pasos del bot unificado (worker + entradas del poller)."""
    import datetime
    try:
        with open(os.path.join(BASE, "hooks", "logs", "prompt-worker.log"), "a") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"),
                                    " ".join(str(x) for x in a)))
    except OSError:
        pass
HEADLESS_QUESTIONS_ANSWER_LAST = [None]


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
        import threading as _th
        qs = params.get("questions") or []
        rid = params.get("requestId") or str(time.time())
        ev = _th.Event()
        HEADLESS_QUESTIONS[rid] = {"event": ev, "answer": None, "qs": qs,
                                   "titulo": titulo}
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
        ev.wait(timeout=480)
        HEADLESS_QUESTIONS.pop(rid, None)
        if ev.is_set():
            return {"action": "accept",
                    "content": {"answer": HEADLESS_QUESTIONS_ANSWER_LAST[0]}}
        send(cfg, "⌛️ La pregunta de «%s» caducó sin respuesta." % esc(titulo))
        return {"action": "decline", "reason": "timeout telegram"}
    return handler


def _queue_path():
    return os.path.join(BASE, "hooks", "state", "bot-queue.json")


def _queue_put(sid, act, text):
    try:
        try:
            with open(_queue_path()) as f:
                q = json.load(f)
        except (OSError, ValueError):
            q = {}
        lst = q.get(sid) or []
        lst.append({"act": act, "text": text[:6000]})
        q[sid] = lst[-5:]
        with open(_queue_path(), "w") as f:
            json.dump(q, f, ensure_ascii=False)
    except OSError:
        pass


def _queue_take(sid):
    try:
        with open(_queue_path()) as f:
            q = json.load(f)
        lst = q.pop(sid, [])
        with open(_queue_path(), "w") as f:
            json.dump(q, f, ensure_ascii=False)
        return lst
    except (OSError, ValueError):
        return []


def _live_view(c, cfg, sid, t0, titulo, msg_id, th):
    """Sidecar de un turno headless: drena session/events (delta stream —
    reasoning_delta/text_delta son lo ÚNICO que streamea en vivo:
    session/messages recién entrega el mensaje cuando el paso completa) y
    edita el placeholder de Telegram. Cada llamada devuelve el buffer
    acumulado desde el subscribe: dedupe por eventId y NO acumular payloads
    (guardas de tamaño — un buffer gordo infló el proceso a GBs).
    Corta cuando el turno termina (th muere) o a los 35 min. Nunca lanza
    ni toca la entrega final."""
    razon = ""
    texto = ""
    n_deltas = 0
    vistos = set()
    last_edit = 0.0
    deadline = time.time() + 2100
    while time.time() < deadline:
        if not th.is_alive():
            break
        try:
            r = c.request("session/events", {"sessionId": sid}, timeout=20)
            evs = (r.get("result") or {}).get("events") or []
            # buffer re-enviado entero en cada llamada: solo los NUEVOS
            for ev in evs[-400:]:
                eid = ev.get("eventId")
                if not eid or eid in vistos:
                    continue
                vistos.add(eid)
                if len(json.dumps(ev)) > 30000:  # snapshot: ignorar, no guardar
                    continue
                p = ev.get("payload") or {}
                k = p.get("kind") or p.get("type") or ""
                if k == "reasoning_delta" and p.get("delta"):
                    razon += p["delta"]
                    n_deltas += 1
                elif k == "text_delta" and p.get("delta"):
                    texto += p["delta"]
                    n_deltas += 1
                    razon = ""  # empezó a escribir: el CoT ya cumplió
        except Exception:  # noqa: BLE001 - el en vivo jamás rompe el turno
            pass
        if msg_id and time.time() - last_edit >= 3.0:
            last_edit = time.time()
            trans = int(time.time() - t0)
            if texto:
                cuerpo = ("⚙️ <b>«%s»</b> · %ds\n<pre>%s</pre>\n✍️ escribiendo…"
                          % (esc(titulo), trans, esc(texto[-700:])))
            elif razon:
                cuerpo = ("🧠 <b>«%s»</b> · %ds\n<b>pensando:</b>\n<pre>%s</pre>"
                          % (esc(titulo), trans, esc(razon[-700:])))
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
        time.sleep(2.0)
    pwlog("live fin: deltas=%d razon=%d texto=%d" % (n_deltas, len(razon), len(texto)))


def prompt_worker(cfg, act, text):
    """Corre un prompt headless en la conversación activa y empuja la
    respuesta a Telegram. Vive en un thread del poller."""
    import sys as _sys
    import threading as _th
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import zcode_proto as zp
    sid, ws, titulo = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    with _WORKERS_LOCK:
        if sid and sid in HEADLESS_WORKERS:
            # ya hay un worker para la sesión (carrera poller/queue_drain):
            # encolar sin avisar — el que corre hace drain al terminar
            _queue_put(sid, act, text)
            return
        snap_sel = None
        HEADLESS_WORKERS[sid or "nueva"] = {"t0": time.time(), "prompt": text[:80],
                                            "ws": ws, "titulo": titulo, "client": None}
    pwlog("worker start sid=%s titulo=%s rss=%dMB texto=%r"
          % (sid, titulo, _rss_mb(), text[:60]))
    try:
        c = zp.ProtoClient(cwd=ws,
                           stderr_path=os.path.join(BASE, "hooks", "logs",
                                                    "proto-stderr.log"),
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
        HEADLESS_WORKERS.pop(sid, None)
        send(cfg, "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120]))
        return
    # el cliente queda registrado: /stop frenaría la corrida con ÉL (mismo
    # runtime = stop real; otro runtime no ve turnos ajenos)
    HEADLESS_WORKERS[sid or "nueva"]["client"] = c
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
            with _WORKERS_LOCK:  # re-key: el botón de /stop apunta al sid real
                ent = HEADLESS_WORKERS.pop("nueva", None)
                if ent is not None:
                    HEADLESS_WORKERS[sid] = ent
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
                      {"sessionId": sid, "content": text,
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
            _live_view(c, cfg, sid, t0, titulo, ph, th)
            fin = res.get("fin")
            if fin is None:  # la vista cortó por deadline: esperar el resto
                fin = _turn_end_poll(sid, t0, timeout=600)
        else:
            fin = _turn_end_poll(sid, t0, timeout=1800)
        pwlog("fin:", (fin or {}).get("event"), "rss=%dMB" % _rss_mb())
        if ph:
            if fin is None:
                _live_edit(cfg, ph, "⏱️ <b>«%s»</b> sigue corriendo — /status"
                           % esc(titulo))
            elif fin.get("event") == "turn.failed":
                _live_edit(cfg, ph, "❌ <b>«%s»</b> falló — detalle abajo"
                           % esc(titulo))
            else:
                _live_edit(cfg, ph, "✅ <b>«%s»</b> · respuesta:" % esc(titulo))
        if fin is None:
            send(cfg, "⏱️ «%s» sigue corriendo tras 30 min — sigue de fondo; /status." % esc(titulo))
            return
        if fin.get("event") == "turn.failed":
            err = str((fin.get("error") or {}).get("message", ""))[:150]
            send(cfg, "❌ «%s» falló: %s" % (esc(titulo), esc(err)))
            return
        if (HEADLESS_WORKERS.get(sid) or {}).get("stopped"):
            if ph:
                _live_edit(cfg, ph, "⏹️ <b>«%s»</b> frenado" % esc(titulo))
            send(cfg, "⏹️ «%s» frenado por /stop." % esc(titulo))
            return
        dur = (time.time() - t0) / 60.0
        msgs = _fetch_messages(c, sid, after_id=last_id, limit=60) or []
        texto = _extract_assistant_text(msgs).strip()
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
        HEADLESS_WORKERS.pop(sid or "nueva", None)
        try:
            c.close()
            if sid and snap_sel:
                _restore_model(sid, snap_sel)
        except Exception:
            pass
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
        ("quota", "credits del coding plan"),
        ("usage", "consumo de hoy por sesión"),
        ("last", "reenviar la última respuesta completa"),
        ("help", "todos los comandos"),
    ]
    try:
        tg_api(cfg, "setMyCommands",
               {"commands": [{"command": c, "description": d} for c, d in cmds]})
        return True
    except Exception as exc:  # noqa: BLE001
        log("errors", "setMyCommands: %s" % exc)
        return False


def handle_free_text(cfg, text):
    """Texto libre fuera de esperas: si hay una ❓ headless pendiente la
    responde; si no, va a la conversación activa (headless)."""
    import threading as _th
    # 1) respuesta a una pregunta del agente headless
    for rid, q in list(HEADLESS_QUESTIONS.items()):
        if not q["event"].is_set():
            resp = text.strip()
            qs = q.get("qs") or []
            # numero -> label/value de la opcion
            if resp.isdigit() and qs:
                try:
                    ops = (qs[0].get("options") or [])
                    op = ops[int(resp) - 1]
                    resp = op.get("value") or op.get("label") or resp
                except (IndexError, ValueError):
                    pass
            HEADLESS_QUESTIONS_ANSWER_LAST[0] = resp
            q["event"].set()
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


def handle_command(cfg, text):
    """Comandos del 2_bot. Devuelve la respuesta (HTML) o None si no es comando."""
    t = norm(text)
    if t in ("/start", "start"):
        return "🤖 Bot de control de ZCode activo.\nComandos: /quota — /ping — /help"
    if t in ("/quota", "quota"):
        return quota_report()
    if t in ("/usage", "uso", "/uso", "usage"):
        return usage_report()
    if t in ("/ctx", "ctx", "contexto"):
        return context_report()
    if t == "/compact" or t.startswith("/compact "):
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
        if not rest:
            return compact_list_report("projects")
        return _compact_args(rest, "projects")
    if t in ("/all_compact", "/allcompact", "all_compact") \
            or t.startswith("/all_compact "):
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
        if not rest:
            return compact_list_report("all")
        return _compact_args(rest, "all")
    if (t in ("/stop", "/all_stop", "/allstop", "stop", "all_stop")
            or t.startswith("/stop ") or t.startswith("/all_stop ")):
        # /stop SOLO maneja corridas iniciadas desde el celular (HEADLESS_WORKERS):
        # lo que corre en la Mac se frena con el botón Stop de la app, acá ni
        # se lista (no confundir).
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
        if rest.startswith("@ses"):
            return bot_stop(rest.split()[0][1:])
        if rest.isdigit():
            items = list(HEADLESS_WORKERS.items())
            i = int(rest)
            if 1 <= i <= len(items):
                sid, w = items[i - 1]
                return bot_stop(sid, w.get("titulo"))
            return "⚠️ Número fuera de la lista (1 a %d) — mandá /stop." % len(items)
        return stop_list_report()
    if t == "/task" or t.startswith("/task "):
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
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
    if t in ("/all_tasks", "/alltasks", "/tasks", "all_tasks", "tasks") \
            or t.startswith("/all_tasks "):
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
        if rest.isdigit():
            return task_select(int(rest), "all")
        return task_list_report("all")
    if t in ("/status", "status"):
        return status_report()
    if t == "/last":
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
    if t == "/run" or t.startswith("/run "):
        import threading as _th
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
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
    if t == "/model" or t.startswith("/model "):
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
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
    if t == "/thinking" or t.startswith("/thinking "):
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
        act = active_bot_session()
        if not act:
            return "⚠️ Elegí conversación primero (/task n o /task new)."
        sel = _cur_sel(act)
        titulo = act.get("title") or "?"
        mid = sel.get("modelId", "?")
        th = (sel.get("options") or {}).get("reasoningLevel", "high")
        if not rest:
            fila = [{"text": ("● " if th == lv else "○ ") + lv,
                     "callback_data": "th:" + lv} for lv in THINKING_LEVELS]
            return ("🎯 «%s» · 🤖 %s\n🧠 Thinking: %s\n"
                    "Tocá para cambiar (aplica en el próximo turno):"
                    % (esc(titulo), esc(mid), esc(th)),
                    {"inline_keyboard": [fila]})
        lv = rest.lower()
        if lv not in THINKING_LEVELS:
            return "⚠️ Niveles: %s" % " · ".join(THINKING_LEVELS)
        _set_sel(act, thinking=lv)
        return ("🎯 «%s»\n🧠 Thinking: %s · modelo %s (aplica en el próximo turno)"
                % (esc(titulo), esc(lv), esc(mid)))
    if t == "/mode" or t.startswith("/mode "):
        rest = text.strip().split(" ", 1)[1].strip() if " " in text.strip() else ""
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
    if t == "/fork":
        act = active_bot_session()
        if not act or not act.get("sid"):
            return "⚠️ Elegí conversación primero (/task n)."
        import sys as _s2
        _s2.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import zcode_proto as zp
        try:
            c = zp.ProtoClient(cwd=act["ws"])
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
    if t == "/api" or t.startswith("/api "):
        import sys as _s3
        _s3.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import zcode_proto as zp
        partes = text.strip().split(" ", 2)
        if len(partes) < 2:
            return "⚠️ /api &lt;método&gt; [json de params]"
        metodo = partes[1]
        params = {}
        if len(partes) == 3 and partes[2].strip():
            try:
                params = json.loads(partes[2])
            except ValueError as e:
                return "⚠️ JSON inválido: %s" % esc(str(e)[:100])
        try:
            c = zp.ProtoClient(cwd=active_workspace_path())
        except OSError as e:
            return "❌ " + esc(str(e)[:100])
        try:
            r = c.request(metodo, params, timeout=60)
            return "🔌 %s\n%s" % (esc(metodo), esc(json.dumps(r, ensure_ascii=False)[:2800]))
        finally:
            c.close()
    if t == "/ping":
        return "🏓 pong"
    if t == "/help":
        return ("🤖 <b>Bot unificado ZCode</b> (headless — sirve con la Mac bloqueada)\n"
                "<b>Conversaciones</b>\n"
                "/task — con Project · /task n elige · /task new [proyecto]\n"
                "/all_tasks — todas, incluidas las sueltas del grupo Tasks\n"
                "texto libre — corre en la conversación activa\n"
                "/run [proyecto] prompt — nueva conversación + prompt\n"
                "/model — modelo de la activa · /thinking — max/high/low\n"
                "/mode — yolo|build|edit|plan|guarded\n"
                "/fork — bifurcar la activa · /last — última respuesta completa\n"
                "<b>Control</b>\n"
                "/quota — credits 5h + semanal del plan\n"
                "/usage — detalle de hoy por sesión\n"
                "/ctx — tasks con % de contexto\n"
                "/ping — prueba de vida\n"
                "/status — qué está corriendo + conversación activa\n"
                "/stop — frená tus corridas del celular (solo esas, botones)\n"
                "/compact — lista Projects para compactar · /all_compact todas\n"
                "<b>Info</b>\n"
                "/ping — prueba de vida\n"
                "(permisos 🔐 y preguntas ❓ se responden tocando los botones, o con texto/número)")
    return None


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
    return any(not q["event"].is_set() for q in HEADLESS_QUESTIONS.values())


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
            if os.path.exists(_reload_now_path()) and not HEADLESS_WORKERS:
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
