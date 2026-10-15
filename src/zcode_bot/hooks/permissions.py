"""hooks.permissions — manejo de PermissionRequest y AskUserQuestion."""
import json
import os
import re
import time
import hashlib

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import preview, esc, labeled
from zcode_bot.utils.validation import first
from zcode_bot.utils.logging import log, log_payload, pwlog
from zcode_bot.utils.http import send

from zcode_bot import state as zc_state
from zcode_bot import concurrency as zc_conc
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS
from zcode_bot.state.manager import _task_current_ok, _worker_owned, _perm_key, _perm_replay


_PERM_REPLAY_S = 600


def _perm_key(sid, name, inp):
    try:
        cuerpo = json.dumps(inp, sort_keys=True, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        cuerpo = preview(inp, 500)
    return "%s|%s|%s" % (sid or "?", name,
                         hashlib.sha1(cuerpo.encode("utf-8")).hexdigest()[:16])


def _perm_remember(key, decision):
    def fn(d):
        d = d if isinstance(d, dict) else {}
        d[key] = {"ts": time.time(), "decision": decision}
        if len(d) > 50:
            for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:-50]:
                d.pop(k, None)
        return d
    try:
        from zcode_bot.state import _store
        _store("bot-perm.json").update_cross_process(fn, {})
    except OSError:
        pass


def _perm_replay(key):
    from zcode_bot.state import _store
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


def tool_desc(payload):
    name = first(payload, "tool_name", "toolName", "tool") or "herramienta"
    inp = first(payload, "tool_input", "toolInput", "input") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"input": inp}
    return str(name), inp