"""hooks — los entrypoints que dispara el CLI de ZCode (F4j).

Cada on_* es un proceso corto que lee el payload por stdin, notifica al
dueño y (para permisos/preguntas) espera la respuesta por el chat con
wait_reply. Los cmd_* son los entrypoints CLI largos (register,
stuck-watch, watch-app). Permiso con replay anti-loop (incidente 13/10).
Transporte POR ATRIBUTO (uhttp.*).
"""
import json
import os
import re
import subprocess
import sys
import time

from zcode_bot import config
from zcode_bot import fswatch
from zcode_bot.limites import CH_TURNO, PARTES_MAX
from zcode_bot.core import (_hook_sid, _task_current_ok, _worker_owned,
                            _perm_key, _perm_remember, _perm_replay,
                            _workers, _questions, _queue_peek)
from zcode_bot.updates import (current_offset, fetch_updates, save_offset)
from zcode_bot.workers import _queue_resume
from zcode_bot.commands.dispatch import wait_reply
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview, labeled
from zcode_bot.utils.validation import first
from zcode_bot.utils.config import load_config, save_config, configured
from zcode_bot.utils.logging import log, log_payload


def tool_desc(payload):
    name = first(payload, "tool_name", "toolName", "tool") or "herramienta"
    inp = first(payload, "tool_input", "toolInput", "input") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"input": inp}
    return str(name), inp


def emit(obj):
    """Imprime la salida JSON del hook (schema estricto) y termina."""
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


# mudada a config (F34b): UNA definición del nombre; acá queda el
# re-export que usan gateway y espectador
from zcode_bot.config import marker_path  # noqa: E402  F34b


def _turno_completo(cfg, cabecera, texto, partes_max=PARTES_MAX):
    """F32 — pedido de Damian: el turno de la Mac llega COMPLETO al celu,
    no en vista previa. Trocea a ~3800 chars; solo la primera parte suena
    (una notificación, colas mudas). Si el texto excede partes_max, la
    última marca el recorte — el texto íntegro queda en la sesión (/cot)."""
    trozos = [texto[i:i + CH_TURNO] for i in range(0, len(texto), CH_TURNO)] or [""]
    cortado = len(trozos) > partes_max
    resto = 0
    if cortado:
        resto = sum(len(t) for t in trozos[partes_max:])
        trozos = trozos[:partes_max]
    for i, t in enumerate(trozos):
        cola = " (%d/%d)" % (i + 1, len(trozos)) if len(trozos) > 1 else ""
        if cortado and i == len(trozos) - 1:
            cola += ("\n(+%d caracteres más — quedaron en la sesión, "
                     "/cot tiene el trace)" % resto)
        uhttp.send(cfg, (cabecera if i == 0 else "···") + "\n" + esc(t) + cola,
                   loud=(i == 0))


def _datos_turno(payload, t0=None):
    """Línea de datos pertinentes del turno. Arranca con el ORIGEN: los
    hooks solo corren para turnos de la app (los del bot los reporta el
    worker con su propio formato) — explícito porque el workspace puede
    llamarse "bot" y confundir (pedido de Damian, 19/10)."""
    datos = ["🖥️ Mac"]
    pasos = payload.get("toolCallCount")
    try:
        pasos = int(pasos)
    except (TypeError, ValueError):
        pasos = None
    if pasos is not None:
        datos.append("🧰 %d paso%s" % (pasos, "s" if pasos != 1 else ""))
    if t0:
        datos.append("⏱ %.0f min" % max(0, (time.time() - t0) / 60))
    # workspace con su padre: un proyecto llamado "bot" a secas se leía
    # como el ORIGEN del mensaje y salía "Mac - Bot" (19/10) — con la
    # ruta relativa se ve que es una carpeta
    partes = [p for p in str(payload.get("cwd") or "").rstrip("/").split("/")
              if p]
    ws = "/".join(partes[-2:]) if partes else ""
    if ws:
        datos.append("📁" + esc(ws))
    return (" · " + " · ".join(datos)) if datos else ""


def on_user_prompt(cfg, payload):
    prompt = str(first(payload, "prompt", "text", "message") or "")
    session = str(first(payload, "session_id", "sessionId") or "ses")
    token = f"{time.time_ns()}"
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(marker_path(session), "w") as f:
        f.write(json.dumps({"token": token, "prompt": preview(prompt, 200)}))
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
        # F33: espectador — el turno de la Mac streamea al celu (cajas de
        # tools + narración por el rollout; la respuesta final la manda
        # on_stop/F32). Solo task current, sin worker (mismo gate de arriba)
        if cfg.get("mac_espectador", True) and _task_current_ok(cfg, session):
            subprocess.Popen(
                [sys.executable, me, "--espectador", session,
                 str(payload.get("cwd") or os.getcwd())],
                start_new_session=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if cfg.get("notify_prompt") and _task_current_ok(cfg, session):
            # F32: prompt COMPLETO (antes: preview de 200)
            _turno_completo(cfg, labeled(payload, "▶️ <b>Turno iniciado</b>"
                                         + _datos_turno(payload)), prompt)


def on_stop(cfg, payload):
    session = str(first(payload, "session_id", "sessionId") or "ses")
    t0 = None  # F32: mtime del marker = arranque del turno (duración)
    try:
        t0 = os.path.getmtime(marker_path(session))
    except OSError:
        pass
    try:
        os.remove(marker_path(session))
    except OSError:
        pass
    # F17: la Mac liberó la task — si quedaron mensajes encolados (mandados
    # con la task ocupada), despertar la cola YA (auto-resume). Solo para
    # sesiones sin worker del bot (el drain del worker es el dueño de ese
    # relay) y solo la task current (F15: el resto duerme hasta /task).
    try:
        if _queue_peek(session) and _workers().get(session) is None \
                and _task_current_ok(cfg, session):
            res = _queue_resume(load_config(), session)
            if res:
                uhttp.send(cfg, res, loud=False)
    except Exception:  # noqa: BLE001 - el hook jamás rompe la sesión
        pass
    if not cfg.get("notify_stop"):
        return
    if _worker_owned(session) or not _task_current_ok(cfg, session):
        return  # F15: el marker se limpia igual (side-effect ≠ notificación)
    # F32: respuesta COMPLETA (antes: preview de 500). responseText trae el
    # texto íntegro del turno; responsePreview queda al final por si algún
    # evento solo trae la versión corta
    resp = str(first(payload, "responseText", "response",
                     "last_assistant_message", "stop_response",
                     "responsePreview") or "(sin respuesta)")
    _turno_completo(cfg, labeled(payload, "✅ <b>Turno terminado</b>"
                                 + _datos_turno(payload, t0=t0)), resp)


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
        uhttp.send(cfg, "👍 Ya aprobado hace un momento — reenviando la decisión.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow"}}})
        return
    if previa == "deny":
        uhttp.send(cfg, "🚫 Ya rechazado hace un momento — reenviando la decisión.",
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
    uhttp.send(cfg, labeled(payload, "\n".join(lines)),
         markup={"inline_keyboard": kb})
    kind, text = wait_reply(cfg, "permiso pendiente")
    if kind == "yes":
        _perm_remember(key, "allow")
        uhttp.send(cfg, "👍 Aprobado desde Telegram — el plan queda en ejecución.",
             loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "allow"}}})
        return
    if kind == "no":
        _perm_remember(key, "deny")
        uhttp.send(cfg, "🚫 Rechazado desde Telegram", loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {"behavior": "deny",
                                                  "message": "Denegado por el usuario desde Telegram"}}})
        return
    if kind == "text" and (text or "").strip() == "planfix":
        # ✏️ ajustes al plan: pedir el texto y rechazar con ese feedback
        uhttp.send(cfg, "✏️ Escribí qué querés que cambie del plan (cualquier "
                  "comando / cancela y deja el plan pendiente):", loud=False)
        kind2, texto2 = wait_reply(cfg, "ajustes del plan")
        feed = (texto2 or "").strip() if kind2 == "text" else ""
        if not feed or feed.startswith("/"):
            uhttp.send(cfg, "🙅 Sin ajustes — quedó pendiente en la app.", loud=False)
            return
        _perm_remember(key, "deny")
        uhttp.send(cfg, "✏️ Ajustes enviados al agente: %s"
             % esc(preview(feed, 200)), loud=False)
        emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                     "decision": {
                                         "behavior": "deny",
                                         "message": ("El usuario pide ajustes al plan: %s"
                                                     " — actualizalo y volvé a proponerlo."
                                                     % feed)}}})
        return
    if kind is None:
        uhttp.send(cfg, "⏱ Sin respuesta por Telegram: quedó el diálogo en la app (si ya respondiste ahí, ignorá esto).")
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
        uhttp.send(cfg, "📨 Ya respondida hace un momento — reenviando: %s"
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
        uhttp.send(cfg, labeled(payload, "\n".join(lines)))
        return
    lines.append(f"\nRespondé con el número (o tocá un botón; esperando {int(cfg.get('question_wait_timeout_s', 60))} s, después también aparece en la Mac)")
    kb = []
    q0 = questions[0] if questions else {}
    for j, opt in enumerate((q0.get("options") or [])[:4], 1):
        label = opt.get("label", "?") if isinstance(opt, dict) else str(opt)
        kb.append([{"text": ("%d. %s" % (j, label))[:36], "callback_data": "ans:%d" % j}])
    uhttp.send(cfg, labeled(payload, "\n".join(lines)),
         markup={"inline_keyboard": kb} if kb else None)
    qcfg = dict(cfg, wait_timeout_s=int(cfg.get("question_wait_timeout_s", 60)))
    kind, text = wait_reply(qcfg, "pregunta pendiente")
    if kind is None:
        uhttp.send(cfg, "⏱ Sin respuesta: la pregunta quedó en la app.")
        return  # sin decisión: la pregunta se muestra en la app
    m = re.match(r"^([0-9]+)\b", text or "")
    if m:
        answer = f"opción {m.group(1)}"
    elif kind in ("yes", "no"):
        answer = "sí" if kind == "yes" else "no"
    else:
        answer = text or ""
    uhttp.send(cfg, f"📨 Respuesta enviada: {esc(preview(answer, 200))}", loud=False)
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
    uhttp.send(cfg, labeled(payload, f"❌ <b>Falló</b> {esc(name)}: {esc(preview(err, 220))}"))


def on_session_start(cfg, payload):
    if not cfg.get("notify_session"):
        return
    sid = _hook_sid(payload)
    if _worker_owned(sid) or not _task_current_ok(cfg, sid):
        return  # F15: cada turno headless hace resume — eso no es novedad
    source = str(first(payload, "source", "reason", "trigger") or "startup")
    emoji = {"startup": "🟢", "resume": "🔄", "clear": "🧹", "compact": "📦"}.get(source, "🟢")
    uhttp.send(cfg, labeled(payload, f"{emoji} <b>Sesión</b> ({esc(source)})"))


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


def cmd_register():
    cfg = load_config()
    if not cfg.get("token"):
        print("Falta el token: zcode-telegram.py set-token <TOKEN>")
        sys.exit(1)
    print("Mandale un mensaje al bot desde Telegram (espero hasta 120 s)…")
    deadline = time.time() + 120
    offset = current_offset()
    while True:
        rest = deadline - time.time()
        if rest <= 0:
            break
        try:
            # long-poll: la espera reactiva la sostiene el server de
            # Telegram (hasta min(rest, 25) s) — nada de sondear con sleeps
            offset, batch, _n = fetch_updates(cfg, offset, min(rest, 25))
        except Exception as exc:  # noqa: BLE001
            log("errors", f"getUpdates falló: {exc}")
            continue
        save_offset(offset)
        if batch:
            msg = batch[-1]
            cfg["chat_id"] = msg["chat"]["id"]
            cfg["owner_user_id"] = msg["from"]["id"]
            cfg["owner_name"] = msg["from"].get("first_name") or msg["from"].get("username") or ""
            save_config(cfg)
            uhttp.send(cfg, f"✅ Registrado, {cfg['owner_name']}. Los avisos de ZCode llegan acá.")
            print(f"OK chat_id={cfg['chat_id']} user={cfg['owner_name']}")
            return
    print("No llegó ningún mensaje. Reintentá.")
    sys.exit(1)


def cmd_stuck_watch(token, session):
    cfg = load_config()
    minutes = int(cfg.get("stuck_after_min", 20))
    deadline = time.time() + minutes * 60
    
    # Usar fswatch.wait_for reactivo en lugar de polling con sleep(30)
    # El marker file se borra cuando el turno termina (Stop)
    marker = marker_path(session)
    
    def marker_gone():
        return not os.path.exists(marker)
    
    def token_changed():
        try:
            with open(marker) as f:
                current = json.load(f).get("token")
            return current != token
        except (OSError, ValueError):
            return True
    
    def condition():
        if marker_gone() or token_changed():
            return True
        return time.time() >= deadline
    
    # wait_for usa fswatch hooks reactivos - cero CPU en espera
    fswatch.wait_for(condition, [marker], timeout=minutes * 60, poll=30)
    
    # Verificar por qué salió
    if time.time() >= deadline:
        try:
            with open(marker) as f:
                prompt = json.load(f).get("prompt", "")
        except (OSError, ValueError):
            prompt = ""
        if not _task_current_ok(cfg, session):
            return
        uhttp.send(cfg, f"⚠️ <b>Posible sesión trabada</b> hace {minutes} min.\nTurno: {esc(preview(prompt, 200))}")


def cmd_watch_app():
    cfg = load_config()
    state_file = os.path.join(config.STATE_DIR, "app-running")
    try:
        with open(state_file) as f:
            was = f.read().strip() == "1"
    except OSError:
        was = None
    
    # Usar fswatch.wait_change reactivo en lugar de polling con sleep(60)
    # Vigilamos el proceso ZCode - cuando cambie (inicio/fin), fswatch nos despierta
    while True:
        # Verificar estado actual
        up = subprocess.run(["pgrep", "-x", "ZCode"], capture_output=True).returncode == 0
        if was is not None and up != was:
            uhttp.send(cfg, "🔴 <b>ZCode se cerró</b>" if not up else "🟢 <b>ZCode volvió a arrancar</b>")
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(state_file, "w") as f:
            f.write("1" if up else "0")
        was = up
        
        # Espera reactiva: fswatch.wait_change vigila el proceso via pgrep state
        # Usamos un archivo sentinela que cambia cuando el proceso cambia
        # Como fallback simple, usamos wait_change en directorio vacío con timeout
        # (En producción ideal: vigilar /proc o usar kqueue EVFILT_PROC)
        sentinel = os.path.join(config.STATE_DIR, "app-watch-sentinel")
        try:
            os.utime(sentinel, None)
        except OSError:
            pass
        # Espera hasta 60s o hasta que cambie el sentinel (lo tocamos al detectar cambio)
        fswatch.wait_change([sentinel], timeout=60, poll=5)
