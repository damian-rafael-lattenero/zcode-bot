"""hooks.questions — manejo de AskUserQuestion (preguntas ❓)."""
import json
import os
import re
import time

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import preview, esc, labeled
from zcode_bot.utils.validation import first
from zcode_bot.utils.logging import log, log_payload, pwlog
from zcode_bot.utils.http import send

from zcode_bot import state as zc_state
from zcode_bot import concurrency as zc_conc
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS
from zcode_bot.state.manager import _task_current_ok, _q_gemela


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


# Imports tardíos para evitar ciclos
import os
import json
import time

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import preview, esc, labeled
from zcode_bot.utils.validation import first
from zcode_bot.utils.logging import log, log_payload, pwlog
from zcode_bot.utils.http import send

from zcode_bot import state as zc_state
from zcode_bot import concurrency as zc_conc
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS
from zcode_bot.state.manager import _task_current_ok, _q_gemela
from zcode_bot.hooks.permissions import _perm_key, _perm_replay