"""hooks.lifecycle — eventos de ciclo de vida de ZCode."""
import os
import json
import time
import subprocess

from zcode_bot.utils.config import load_config, _task_current_ok
from zcode_bot.utils.formatting import preview, esc
from zcode_bot.utils.logging import log, log_payload, pwlog
from zcode_bot.utils.http import send

from zcode_bot import state as zc_state
from zcode_bot import proto as zc_proto
from zcode_bot import fswatch


def marker_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "ses")
    return os.path.join(STATE_DIR, f"turn-{safe}.marker")


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
                          "zcode-telegram.py")
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


def on_session_start(cfg, payload):
    if not cfg.get("notify_session"):
        return
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: cada turno headless hace resume — eso no es novedad
    source = str(first(payload, "source", "reason", "trigger") or "startup")
    emoji = {"startup": "🟢", "resume": "🔄", "clear": "🧹", "compact": "📦"}.get(source, "🟢")
    send(cfg, labeled(payload, f"{emoji} <b>Sesión</b> ({esc(source)})"))


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


# Imports tardíos para evitar ciclos
import os
import re
import sys
import time

from zcode_bot.utils.config import load_config, STATE_DIR, _task_current_ok
from zcode_bot.utils.formatting import preview, first, esc, labeled
from zcode_bot.utils.http import send
from zcode_bot.utils.logging import log, log_payload, pwlog

from zcode_bot import state as zc_state
from zcode_bot import proto as zc_proto
from zcode_bot import fswatch
from zcode_bot import commands as zc_commands
from zcode_bot import reports as zc_reports

from zcode_bot.hooks.permissions import on_permission_request
from zcode_bot.hooks.questions import on_ask_user_question, _worker_owned, _task_current_ok
from zcode_bot.state import WORKERS, _queue_peek, _queue_resume