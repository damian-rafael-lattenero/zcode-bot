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
import secrets
import subprocess
import sys
import time
import traceback
import urllib.request
import asyncio

# Paquete núcleo (F2): state atómico, concurrencia con lock disciplinado,
# factory de protocolo, scanner de logs y repository sqlite.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zcode_bot import proto as zc_proto        # noqa: E402
from zcode_bot import cli_logs as zc_logs      # noqa: E402
from zcode_bot import tasks_repo as zc_repo    # noqa: E402
from zcode_bot import fswatch                  # noqa: E402
from zcode_bot import commands as zc_commands  # noqa: E402
from zcode_bot import reports as zc_reports    # noqa: E402
from zcode_bot import webhook_server as zc_webhook  # noqa: E402

# Utils (Fase 1): config, formatting, validation, http, logging
from zcode_bot.utils.config import load_config, save_config, DEFAULTS, configured
from zcode_bot.utils.formatting import esc, preview, fmt_tok, fmt_num, datetime_dt, labeled
from zcode_bot.utils.validation import classify, first, norm
from zcode_bot.utils.http import (tg_api, send, send_document, _live_send,
                                  _live_edit, outbox_flush)
from zcode_bot.utils.logging import log, pwlog, log_payload, _log_rotate
from zcode_bot.utils.config import load_config, save_config, DEFAULTS, configured

# Paths: SIEMPRE por atributo (config.STATE_DIR) — la fuente única es
# zcode_bot.config y el fixture de tests rebindea ESE módulo (F1e).
# Los cuerpos de load/save/send/log/outbox viven en utils (un solo cuerpo
# vivo; antes el gateway los redefinía y pisaba los imports).
from zcode_bot import config
from zcode_bot.reporting import (  # noqa: E402  informes (F4f)
    session_titles, coding_usage, read_quota_cache, quota_live, quota_report,
    usage_report, CONTEXT_WINDOWS, _ctx_tasks, _compactiones, _ctx_live,
    context_report, compact_list_report, _compact_args, zcode_compact,
    _tasks_scope, task_list_report, stop_list_report, build_trace,
    _turn_trace, _send_long_reply, status_report, _uptime_txt, _modo_label,
    _resolve_ws, MODOS, _POLLER_T0)
from zcode_bot.nuclear import (  # noqa: E402  ☢️ Nuclear 2.0 (F4e)
    NuclearReport, LAUNCH_LABEL, _NUCLEAR_TTL, _nuc_pids, _nuc_wait, _nuc_kill_own_runtimes,
    app_restart_cycle, _nuclear_flag_path, _nuclear_heartbeat,
    _nuclear_active, _nuclear_run, _nuclear_snapshot, _nuclear_report_path,
    _nuclear_body, _spawn_nuclear_watchdog, cmd_nuclear_watchdog)
from zcode_bot.live_view import (  # noqa: E402  sidecar del turno (F4d)
    LiveTurnView, _turn_end_poll, _poll_cadence, _rss_mb, _rss_now_mb,
    _live_skip_path, _live_skip_all, _live_skip_mark, _live_skip_check,
    _live_skip_clear, _headless_text, _cot_chunks_to_send,
    _cajas_paso_nuevas, HEADLESS_GUARD,
    COT_CHUNK, COT_TAIL, EVENTS_TRIP, LIVE_FRAME_TRIP, LIVE_SKIP_TTL,
    LIVE_BIG_FRAME)
from zcode_bot.messages import (  # noqa: E402  parsers ProtoClient (F4c)
    _extract_assistant_text, _fetch_messages, _last_id_of,
    _steps_of_messages, _tools_of_messages, _tool_desc_corto, _tool_caja,
    _reasoning_of_messages)
from zcode_bot.core import (  # noqa: E402  seam compartido (F4b)
    BotSession, WaitReply,
    _store, _workers, _questions, active_bot_session,
    set_active_bot_session, active_workspace_path, session_title_from_db,
    _hook_sid, _task_current_ok, _worker_owned, _cur_sel, _set_sel,
    _model_snapshot, _restore_model, _proto_ready, _create_session,
    _wait_cli_event, _log_watch_paths, _last_prompt_path, _save_last_prompt,
    running_turns, _queue_peek, _queue_sids_with_items,
    _load_last_prompt, _stop_file, _stop_request, _stop_requested,
    _stop_consume, _perm_key, _perm_remember, _perm_replay, _PERM_REPLAY_S)


# ---------------------------------------------------------------- respuestas

def busy_path():
    return os.path.join(config.STATE_DIR, "tg-busy")


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
    res = tg_api(cfg, "getUpdates", {"offset": offset, "timeout": timeout, "limit": 100},
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


def wait_reply(cfg, hint):
    """Espera una respuesta NUEVA por Telegram hasta wait_timeout_s.

    Devuelve ("yes"|"no"|"text", texto) o (None, None) si se agotó el tiempo.
    Los mensajes que empiezan con "/" son comandos del 2_bot: se responden y
    la espera continúa (no cuentan como respuesta).
    """
    deadline = time.time() + int(cfg.get("wait_timeout_s", 240))
    offset, stale = drain_updates(cfg)  # lo pendiente no cuenta como respuesta
    save_offset(offset)
    os.makedirs(config.STATE_DIR, exist_ok=True)
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
                # Sin sleep(3): el long-poll timeout ya hizo el wait
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
                return WaitReply(kind if kind in ("yes", "no") else "text", text)
            if not batch:
                # Sin sleep(0.5): el long-poll timeout=25 ya bloquea en el server
                pass
        return WaitReply(None, None)
    finally:
        try:
            os.remove(busy_path())
        except OSError:
            pass


# ---------------------------------------------------------------- comandos 2_bot































# Catálogo de modelos/thinking: fuente única en models.py (F1b)
from zcode_bot.models import (  # noqa: E402
    MODELOS, MODEL_SEL, _thinking_levels)


# config.DEFAULT_WS vive en zcode_bot.config (tasks sueltas, «Tasks»)






def task_select(n, scope="projects"):
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    if n < 1 or n > len(tasks):
        return "⚠️ Número fuera de la lista (1 a %d) — mandá /task." % len(tasks)
    t = tasks[n - 1]
    return _task_select_sid(t["sid"], t["title"])


def _task_select_sid(sid, titulo=None):
    """Fija la conversación activa por sessionId (los botones la usan: la
    posición numérica puede repetirse entre workspaces, el sid no)."""
    tasks = {t["sid"]: t for t in _ctx_tasks(30)}
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




def bot_stop(sid, titulo=None):
    """Frena una corrida iniciada DESDE el celular Y limpia su cola
    (pedido F17: /stop también descarta los mensajes encolados de esa
    task). F16: el frenado va por marker — el WORKER lo ve en su loop y
    ejecuta session/stop con su propio runtime (stop real)."""
    n = len(_queue_take(sid))
    tit = _workers().flag_stop(sid)
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
    borrados += viejos(os.path.join(config.STATE_DIR, "bot-stop", "*"), 0.5)
    borrados += viejos(os.path.join(config.STATE_DIR, "turn-*.marker"), 1)
    _log_rotate(os.path.join(config.LOG_DIR, "errors.log"))
    _log_rotate(os.path.join(config.LOG_DIR, "orphan.log"))
    if borrados:
        pwlog("housekeeping: %d artefactos purgados" % borrados)




def _q_gemela(sid_eff, huella):
    """rid de la pregunta IDÉNTICA ya pendiente en el canal (mismo sid y
    mismas questions → misma huella): el re-disparo del CLI la reusa sin
    re-mostrar los botones. None si no hay gemela."""
    for q in _questions().pendings():
        if q.get("sid") == sid_eff and _perm_key(
                sid_eff, "AskUserQuestion",
                {"questions": q.get("questions") or []}) == huella:
            return q.get("request_id")
    return None


def _user_input_handler(cfg, sid, titulo):
    """Callback de interaction/requestUserInput: manda la ❓ al chat y
    espera la respuesta por ARCHIVO (F16: el poller la escribe al tocar
    el botón/número — no hay memoria compartida entre procesos). F15: la
    pregunta lleva SU sid — con varias tasks la respuesta enruta a la que
    preguntó (handle_free_text prioriza la task current)."""
    def handler(params):
        qs = params.get("questions") or []
        sid_eff = sid or (active_bot_session().get("sid"))
        # FIX 14/10 v2 (cuestionarios en loop): el CLI RE-DISPARA la misma
        # AskUserQuestion y el handler re-creaba la entrada y re-mostraba
        # los botones — Damian elegía la opción y volvía a ver la pregunta.
        # (El replay F27 solo cubría el hook del poller, no este camino.)
        # Capas: (1) respondida hace <10 min → devolver directo; (2)
        # idéntica pendiente → reusar su rid SIN re-mostrar; (3) nueva →
        # mostrar.
        huella = _perm_key(sid_eff, "AskUserQuestion", {"questions": qs})
        previa = _perm_replay(huella)
        if previa is not None:
            return {"action": "accept", "content": {"answer": previa}}
        rid = _q_gemela(sid_eff, huella)
        if rid is None:
            rid = str(params.get("requestId") or time.time())
            # /task new: el sid real nace después del spawn — para esa
            # ventana vale el que fija el worker como sesión activa
            _questions().ask(rid, qs, titulo, sid_eff)
            lines = ["❓ <b>%s</b>" % esc(titulo)]
            kb = []
            if len(qs) == 1:
                # una sola pregunta: los botones mapean 1:1 con las opciones
                # que usa la resolución por número (handle_free_text usa qs[0])
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
        resp = _questions().wait(rid, timeout=480)
        if resp is not None:
            _perm_remember(huella, resp)  # el re-disparo no vuelve a preguntar
            return {"action": "accept",
                    "content": {"answer": resp}}
        send(cfg, "⌛️ La pregunta de «%s» caducó sin respuesta." % esc(titulo))
        return {"action": "decline", "reason": "timeout telegram"}
    return handler


def _queue_put(sid, act, text):
    if isinstance(act, BotSession):
        act = act.to_dict()  # la cola persiste dicts (QueueItem en F4g)
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
    if isinstance(act, BotSession):
        act = act.to_dict()  # el mailbox por stdin viaja como dict plano
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")  # shim: entrypoint de subprocesos
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        with open(os.path.join(config.LOG_DIR, "worker-stderr.log"), "a") as err:
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
    atexit.register(lambda: _workers().pop(sid or "nueva"))
    prompt_worker(load_config(), act, text)


def prompt_worker(cfg, act, text):
    """Corre un prompt headless en la conversación activa y empuja la
    respuesta a Telegram. Vive en un PROCESO (F16: actor)."""
    import threading as _th
    sid, ws, titulo = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    key = sid or "nueva"
    abandonado = [False]  # F21: fin=None → el runtime se hereda al supervisor
    if sid and _workers().get(sid) is not None:
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
    _workers().register(key, {"t0": time.time(), "prompt": text[:80],
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
        _workers().pop(key)  # OJO key: pop(None) dejaba el orphan "nueva" (bug F0)
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
            _workers().rekey("nueva", sid)  # el botón de /stop apunta al sid real
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
        if e := zc_proto.proto_err(r, "send"):
            send(cfg, e)
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
            lr = LiveTurnView(c, cfg, sid, t0, titulo, ph, th,
                               last_id=last_id).run()
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
        if _workers().stopped(sid):
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
        _workers().pop(sid or "nueva")
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
        c = zc_proto.make(cwd=ent.get("ws") or config.DEFAULT_WS)
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
                config.BASE, needles=("turn.completed", "turn.failed"),
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
    if _workers().get(sid) is not None:
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
    pend = _questions().first_pending((act or {}).get("sid"))
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
        _questions().answer(pend.get("request_id"), resp)
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




def _cmd_retry(cfg, rest):
    """/retry — relanza el último prompt tal cual (fallo, crash o timeout).
    Si hay una task activa distinta a la del prompt, pregunta... no: corre
    en la task ORIGINAL del prompt (donde tiene contexto)."""
    act, text = _load_last_prompt()
    if not text:
        return "🤔 No tengo un prompt previo para reintentar."
    if _workers().get((act or {}).get("sid") or "nueva") is not None:
        return "⏳ Ya hay un turno corriendo en «%s» — /stop primero si querés."
    if _spawn_worker(cfg, act, text) is None:
        return None
    return "🔁 Reintentando «%s»:\n%s" % (esc((act or {}).get("title", "?")),
                                          esc(preview(text, 200)))




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
        items = _workers().items()
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
        with open(os.path.join(config.BASE, "hooks", "state", "last-reply.txt"),
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
    tasks = {t["sid"]: t for t in _ctx_tasks(30)}
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
        if e := zc_proto.proto_err(rr, "resume"):
            return e
        msgs = _fetch_messages(c, target["sid"], limit=150) or []
        trace = build_trace(target["title"] or target["sid"][-8:],
                            target["sid"], 0.0, msgs)
        try:
            with open(os.path.join(config.STATE_DIR, "last-turn-trace.txt"), "w") as f:
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
        if e := zc_proto.proto_err(r, "fork"):
            return e
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
    if _workers().get(sid) is not None or (sid in running_turns()):
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


def _cmd_set_webhook(cfg, rest):
    """/set_webhook <URL> [secret] — configura webhook de Telegram.
    Sin secret se GENERA uno (token_urlsafe): el receiver es fail-closed
    (sin secret, 401 a todo) — la URL sola nunca basta."""
    if not rest:
        return "⚠️ Uso: /set_webhook <URL> [secret_token]"
    partes = rest.split(" ", 1)
    url = partes[0].strip()
    secret = partes[1].strip() if len(partes) > 1 else secrets.token_urlsafe(32)

    if not url.startswith("https://"):
        return "⚠️ La URL debe ser HTTPS"

    ok = asyncio.run(zc_webhook.set_webhook(cfg, url, secret))
    if ok:
        cfg["webhook_url"] = url
        cfg["webhook_secret"] = secret
        cfg["transport"] = "webhook"  # ADR-011: transportes excluyentes
        save_config(cfg)
        return (f"✅ Webhook configurado: {url}\n"
                f"🔐 secret guardado en config ({secret[:6]}…). "
                f"El poller toma el webhook en su próximo arranque "
                f"(recarga con /deploy go).")
    else:
        return "❌ Falló configuración de webhook"


def _cmd_delete_webhook(cfg, rest):
    """/delete_webhook — elimina webhook y vuelve a long-polling."""
    ok = asyncio.run(zc_webhook.delete_webhook(cfg))
    if ok:
        cfg.pop("webhook_url", None)
        cfg.pop("webhook_secret", None)
        cfg["transport"] = "polling"
        save_config(cfg)
        return "✅ Webhook eliminado (vuelve a long-polling)"
    else:
        return "❌ Falló eliminación de webhook"


def _cmd_webhook_info(cfg, rest):
    """/webhook_info — muestra info del webhook actual."""
    info = asyncio.run(zc_webhook.get_webhook_info(cfg))
    return json.dumps(info, indent=2, ensure_ascii=False)


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
    # Webhook commands
    (("set_webhook", "setwebhook"), _cmd_set_webhook),
    (("delete_webhook", "deletewebhook"), _cmd_delete_webhook),
    (("webhook_info", "webhookinfo"), _cmd_webhook_info),
]:
    REG.register(*_names)(_fn)


def handle_command(cfg, text):
    """Comandos del 2_bot (despacho por Command Registry). Devuelve la
    respuesta (HTML, o (texto, markup)) o None si no es comando."""
    return REG.dispatch(cfg, text)


# ---------------------------------------------------------------- payloads

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


# ---------------------------------------------------------------- eventos

def marker_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "ses")
    return os.path.join(config.STATE_DIR, f"turn-{safe}.marker")


def on_user_prompt(cfg, payload):
    prompt = preview(first(payload, "prompt", "text", "message") or "", 200)
    session = str(first(payload, "session_id", "sessionId") or "ses")
    token = f"{time.time_ns()}"
    os.makedirs(config.STATE_DIR, exist_ok=True)
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
        if _queue_peek(session) and _workers().get(session) is None \
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
            send(cfg, f"✅ Registrado, {cfg['owner_name']}. Los avisos de ZCode llegan acá.")
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
        send(cfg, f"⚠️ <b>Posible sesión trabada</b> hace {minutes} min.\nTurno: {esc(preview(prompt, 200))}")


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
            send(cfg, "🔴 <b>ZCode se cerró</b>" if not up else "🟢 <b>ZCode volvió a arrancar</b>")
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


def _hq_pending():
    return _questions().any_pending()


def _reload_now_path():
    return os.path.join(config.STATE_DIR, "reload-now")


def process_update_sync(cfg, msg):
    """El cuerpo por-update del poller (F2c: extraído de cmd_watch_commands
    — movida PURA, semántica idéntica). Es la pieza que comparten el
    driver síncrono, el AsyncPoller (F2d) y el webhook (F3): un update,
    un procesamiento, en orden y serial. Los `continue` del loop eran
    `return` acá."""
    if not from_owner(cfg, msg):
        return
    if msg.get("chat", {}).get("id") != cfg.get("chat_id"):
        # F22: mensaje del owner desde OTRO chat (grupo): responder allá
        # rompería la unidad del chat. FIX 14/10: con rastro — antes se
        # tragaba en silencio y parecía "el bot no responde".
        pwlog("UPDATE de otro chat %s — ignorado (usá el chat privado)" %
              msg.get("chat", {}).get("id"))
        return
    text = (msg.get("text") or "").strip()
    if not text:
        if msg.get("photo") or msg.get("voice") or msg.get("document"):
            # F23: antes los adjuntos caían en silencio
            send(cfg, "📎 Vi tu adjunto — por ahora proceso "
                      "solo texto: pasame la idea en "
                      "palabras.", loud=False)
        return
    cbd = msg.get("_cb_data") or ""
    if (cbd.startswith("ans:") or cbd in ("yes", "no")) \
            and not _hq_pending():
        # botón de una pregunta que ya no está pendiente:
        # NO inyectar el texto a la conversación activa
        send(cfg, "🤔 Ese botón ya venció — no hay pregunta "
                  "pendiente (/task para la lista).", loud=False)
        return
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


async def runtime_async():
    """Composition root del proceso poller (F3): sobre UN loop asyncio
    corre el transporte elegido por config — polling (default, probado)
    o webhook (ADR-011, mutuamente excluyentes: un solo lector) — más el
    supervisor (recarga/huérfanos/outbox/menú). El webhook comparte el
    pipeline completo: normalize_update → dispatcher serial →
    process_update_sync."""
    from zcode_bot import poller
    from zcode_bot.dispatcher import UpdateDispatcher

    def _clear_reload():
        try:
            os.remove(_reload_now_path())
        except OSError:
            pass

    deps = dict(
        load_config=load_config,
        configured=configured,
        orphans_check=_orphans_check,
        register_menu=register_bot_menu,
        outbox_flush=outbox_flush,
        reload_pending=lambda: os.path.exists(_reload_now_path())
                               and not _nuclear_active(),
        clear_reload=_clear_reload,
        log=log)

    cfg = load_config()
    if cfg.get("transport") == "webhook" and cfg.get("webhook_url"):
        dispatcher = UpdateDispatcher(
            process_update_sync, from_owner,
            on_error=lambda e: log("errors", f"dispatcher: {e}"))
        consumer = asyncio.ensure_future(dispatcher.run())
        runner = await zc_webhook.serve_async(
            cfg, dispatcher, normalize_update,
            lambda: load_config().get("webhook_secret"))
        log("errors", "transporte: webhook (ADR-011) — long-polling OFF")
        try:
            await poller.supervisor_loop(**deps)
        finally:
            await runner.cleanup()
            consumer.cancel()
    else:
        log("errors", "transporte: polling (long-poll 3s)")
        await poller.AsyncPoller(
            fetch=fetch_updates, process=process_update_sync,
            busy_fresh=busy_fresh, current_offset=current_offset,
            save_offset=save_offset, poll_timeout=3,
            on_start=housekeeping, **deps).run_forever()


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
            # F3: composition root async — polling o webhook según config
            # (el loop del poller vive en zcode_bot.poller, testeado con
            # fakes en test_poller.py; el webhook en webhook_server.py)
            asyncio.run(runtime_async())
        else:
            print(__doc__)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - jamás romper la sesión por un bug acá
        log("errors", traceback.format_exc())


if __name__ == "__main__":
    main()
