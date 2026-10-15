"""commands.dispatch — el despacho del 2_bot (F4i).

wait_reply (la espera de respuesta del dueño), la tabla Command Registry
con los ~25 handlers y los dos puntos de entrada (handle_command para
"/comandos", handle_free_text para texto libre → conversación activa).
El prompt del dueño entra por acá y baja a workers._spawn_worker.
Transporte POR ATRIBUTO (uhttp.*).

Los handlers viven intercalados con la tabla (movimiento puro de F4i);
una división por dominio es refactor posterior sobre módulo estable.
"""
import asyncio
import json
import os
import re
import secrets
import subprocess
import threading
import time

from zcode_bot import config
from zcode_bot import commands as zc_commands
from zcode_bot import webhook_server as zc_webhook
from zcode_bot import proto as zc_proto
from zcode_bot.core import (WaitReply, active_bot_session, _load_last_prompt,
                            active_workspace_path, session_title_from_db,
                            set_active_bot_session, _cur_sel, _set_sel,
                            _store, _workers, _questions, running_turns,
                            _queue_peek, _queue_sids_with_items)
from zcode_bot.workers import _spawn_worker, bot_stop, _queue_put, _queue_resume
from zcode_bot.reporting import (build_trace, _send_long_reply, quota_report, usage_report, context_report,
                                 compact_list_report, _compact_args,
                                 task_list_report, stop_list_report,
                                 status_report, _turn_trace, _ctx_tasks,
                                 _tasks_scope, _resolve_ws, _modo_label,
                                 MODOS)
from zcode_bot.nuclear import _nuclear_run
from zcode_bot.updates import (drain_updates, save_offset, busy_path,
                               fetch_updates, from_owner)
from zcode_bot.messages import (_fetch_messages, _extract_assistant_text,
                                _steps_of_messages)
from zcode_bot.models import MODELOS, _thinking_levels
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview, labeled
from zcode_bot.utils.validation import classify, first
from zcode_bot.utils.config import load_config, save_config, configured
from zcode_bot.utils.logging import log


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
                            uhttp.send(cfg, reply[0], loud=False, markup=reply[1])
                        else:
                            uhttp.send(cfg, reply, loud=False)
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
                uhttp.send(cfg, ("✅ <b>Deploy OK</b>\n<pre>%s</pre>" % esc("\n".join(tail)[-1500:]))
                     if ok else
                     ("❌ <b>Deploy falló</b> (rc=%d)\n<pre>%s</pre>"
                      % (p.returncode, esc("\n".join(tail)[-1200:]))),
                     loud=not ok)
            except Exception as exc:
                uhttp.send(cfg, "❌ deploy: %s" % esc(str(exc)[:150]), loud=True)
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
        uhttp.tg_api(cfg, "setMyCommands", params)
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
        ok = uhttp.send_document(cfg, fname, trace,
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
