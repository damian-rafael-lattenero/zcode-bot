"""commands.handlers_tareas — handlers de conversaciones y cola (F9).

Movido TEXTUAL de commands.dispatch (deuda declarada en ADR-012, cierre
F9): task_select/_task_select_sid, handle_free_text, _cmd_retry,
_cmd_stop, _cmd_task, _cmd_all_tasks, _cmd_queue, _queue_mutate,
_cmd_qgo y _cmd_qdel. El prompt del dueño entra por acá y baja a
workers._spawn_worker. Transporte POR ATRIBUTO (uhttp.* vía los seams
de cada módulo dueño).
"""
import os

from zcode_bot import config
from zcode_bot.commands import args
from zcode_bot.core import (active_bot_session, _load_last_prompt,
                            active_workspace_path, session_title_from_db,
                            set_active_bot_session, _store, _workers,
                            _questions, running_turns, _queue_peek,
                            _queue_sids_with_items)
from zcode_bot.workers import _spawn_worker, bot_stop, _queue_put, _queue_resume
from zcode_bot.reporting import (task_list_report, stop_list_report,
                                 _tasks_scope, _resolve_ws, _ctx_tasks)
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.config import load_config


def task_select(n, scope="projects"):
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    if n < 1 or n > len(tasks):
        return args.fuera_de_lista("task", len(tasks))
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
    # y /mode se perdían al cambiar de conversación). F7+++: mode_set
    # también — sin él, cada switch pagaba el setMode de 30s otra vez
    for k in ("model", "mode", "mode_set"):
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
        # numero -> label/value de la opcion (desde 1: un "0" no es opción
        # — con el índice negativo seleccionaba la ÚLTIMA de prepo)
        if resp.isdigit() and qs and int(resp) >= 1:
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
        if not _queue_put(sid, act, text):
            return ("⚠️ La Mac está ocupada y NO pude encolar tu mensaje "
                    "— reintentá en un momento.")
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


def _cmd_stop(cfg, rest):
    """/stop SOLO maneja corridas iniciadas desde el celular: lo que corre
    en la Mac se frena con el botón Stop de la app, acá ni se lista.
    /stop go = freno EN SECO: kill -9 inmediato de todos los runtimes
    del bot (sin esperar el session/stop cooperativo)."""
    r = (rest or "").strip().lower()
    if r in ("go", "hard", "9"):
        items = _workers().items()
        if not items:
            return "🤔 No hay corridas del bot para frenar."
        for sid, w in items:
            bot_stop(sid, w.get("titulo"), grace=0.0)
        return "💀 Freno en seco: kill -9 a %d runtime(s) — el reporte " \
               "llega por el chequeo de huérfanos." % len(items)
    head, _resto = args.head_y_resto(rest)
    sid = args.sid_de(head)
    if sid:
        return bot_stop(sid)
    if head.isdigit():
        items = _workers().items()
        k = int(head)
        if 1 <= k <= len(items):
            sid, w = items[k - 1]
            return bot_stop(sid, w.get("titulo"))
        return args.fuera_de_lista("stop", len(items))
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
            kb.append([
                {"text": "⏫ %d ahora" % i, "callback_data": "qgo:%s:%d" % (sid, i - 1)},
                {"text": "🗑", "callback_data": "qdel:%s:%d" % (sid, i - 1)}])
    return "\n".join(lines), {"inline_keyboard": kb}


def _queue_mutate(sid, fn):
    """F22: read-modify-write ATÓMICO de UNA cola del dict (no pisa las
    demás sesiones). fn(lst) -> nueva lst (o None = sin cambios). True si
    aplicó (auditoría 20/10: el OSError tragado hacía que /qgo diga
    «Lanzado» sin haber sacado el ítem → doble ejecución al drenar)."""

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
        _store(config.ARCH_COLA).update_cross_process(apply, {})
        return True
    except OSError as e:
        from zcode_bot.utils.logging import pwlog
        pwlog("queue_mutate FALLO: %s" % e)
        return False


def _cmd_qgo(cfg, rest):
    """/qgo <sid>:<idx> — lanza YA ese encolado (lo saca de la cola)."""
    parsed = args.parse_sid_idx(rest or "")
    if parsed is None:
        return "⚠️ /qgo sid:idx"
    sid, idx = parsed
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
    if not _queue_mutate(sid, take):
        # sin sacar el ítem, lanzar DUPLICARÍA al drenar la cola
        return ("⚠️ La cola está trabada — no lancé nada (reintentá en "
                "un momento).")
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        _queue_put(sid, item["act"], item["text"])  # no perderlo
        return None
    return "⏫ Lanzado: %s" % esc(preview(item.get("text", ""), 100))


def _cmd_qdel(cfg, rest):
    """/qdel <sid>:<idx> — descarta ese encolado."""
    parsed = args.parse_sid_idx(rest or "")
    if parsed is None:
        return "⚠️ /qdel sid:idx"
    sid, idx = parsed
    item = [[]]

    def drop(l):
        if not (0 <= idx < len(l)):
            return None
        item[0] = l[idx]
        return l[:idx] + l[idx + 1:]
    if not _queue_mutate(sid, drop):
        return "⚠️ La cola está trabada — nada descartado, reintentá."
    if not item[0]:
        return "🤔 Ese encolado ya no está."
    return "🗑 Descartado: %s" % esc(preview(item[0].get("text", ""), 100))


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
