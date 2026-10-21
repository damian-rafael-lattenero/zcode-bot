"""commands.handlers_info — report and menu handlers (F9).

Moved VERBATIM from commands.dispatch (debt declared in ADR-012, F9
close-out): register_bot_menu, _cmd_quota, _cmd_usage, _cmd_ctx,
_cmd_compact, _cmd_all_compact, _cmd_status, _cmd_last, _cmd_cot and
_cmd_help. These are the READ commands: all the heavy lifting lives in
reporting, here we only adapt the reply to the chat.
"""
import os
import re

from zcode_bot import config
from zcode_bot import proto as zc_proto
from zcode_bot import razonamiento
from zcode_bot.core import active_bot_session
from zcode_bot.reporting import (build_trace, _send_long_reply, quota_report,
                                 usage_report, context_report,
                                 compact_list_report, _compact_args,
                                 status_report, _tasks_scope, _ctx_tasks)
from zcode_bot.messages import (_fetch_messages, _extract_assistant_text,
                                _steps_of_messages, colorear_diff)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview, partes_iguales
from zcode_bot.utils.logging import log


def register_bot_menu(cfg):
    """Telegram command menu (the '/' button): re-registered at poller
    startup to keep it up to date. R6: the list is DERIVED from the
    dispatch specs (a single place declares command+description; the
    visible ones go in table order)."""
    from zcode_bot.commands.dispatch import SPECS  # late import: dispatch imports us
    cmds = [(s.names[0], s.menu_desc) for s in SPECS if s.menu_desc]
    try:
        params = {"commands": [{"command": c, "description": d} for c, d in cmds]}
        if cfg.get("chat_id"):
            # D1: menu visible ONLY in the owner's chat (official scope)
            params["scope"] = {"type": "chat", "chat_id": cfg["chat_id"]}
        uhttp.tg_api(cfg, "setMyCommands", params)
        return True
    except Exception as exc:  # noqa: BLE001
        log("errors", "setMyCommands: %s" % exc)
        return False


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


def _cmd_status(cfg, rest):
    # F38: slow commands in flight — the snapshot lives in dispatch, but
    # dispatch imports THIS module (REG table): late import, no cycle
    from zcode_bot.commands import dispatch as zc_dispatch
    return status_report(inflight=zc_dispatch.inflight_snapshot())


# R8: /diff lives in handlers_diff (verbatim move); re-exported
# because dispatch and the tests import it from here.
from zcode_bot.commands.handlers_diff import _cmd_diff  # noqa: E402,F401


def _cmd_cotb(cfg, rest):
    """/cotb <sid> <t0> <n> [part] — triggered by the «CoT R.C» buttons
    (ct:sid:t0:n[:p]). Reads the cache: instant, no runtime. F43c
    (Damian's idea): the content is shown IN the box message — it edits
    that message with the box text and the keyboard intact so navigation
    keeps going; nothing new lands below. F46: with phs (one button box
    PER reasoning block) it edits the message of ITS reasoning block;
    without phs (old boxes) or if the edit fails, the usual single ph
    or a separate message."""
    a = (rest or "").split()
    if len(a) < 3:
        return "🤔 Uso: tocá un botón «CoT n» del razonamiento de un turno."
    try:
        sid, t0, n = a[0], int(a[1]), int(a[2])
        r = razonamiento.mostrar(sid, t0, n, int(a[3]) if len(a) > 3 else 1)
    except ValueError:
        return "🤔 Botón inválido — mandá /cot para el documento completo."
    try:
        import json as _json
        with open(razonamiento.cache_path(sid, t0)) as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return r
    phs = d.get("phs") or []
    # n>=1 (a manual /cotb with 0: the negative index pointed at the
    # last reasoning block — audit 20/10)
    ph_n = phs[n - 1] if n >= 1 and len(phs) >= n and phs[n - 1] else None
    ph = ph_n or d.get("ph")
    if not ph:
        return r
    texto, kb = r if isinstance(r, tuple) else (r, None)
    rotulos = d.get("rotulos") or []
    if rotulos:
        # F49: the keyboard is the one of the MESSAGE being edited — the
        # buttons of the boxes that LIVE in it (its reasoning block, or
        # the late ones from the closing); the full index only in the old
        # single-message cache. Before, the viewer stuffed ALL buttons
        # into any message (Damian: "I tapped 1.1 and buttons from 2.1
        # appeared"). With parts: parts on top and boxes at the bottom —
        # before, parts replaced the keyboard and the message was left
        # with no way back.
        if ph_n:
            idxs = [i + 1 for i, p in enumerate(phs) if p == ph_n]
            filas = razonamiento.botones_indices(sid, t0, rotulos, idxs)
        else:
            filas = razonamiento.botones_cajas(sid, t0, rotulos)
        kb = {"inline_keyboard": (kb or {}).get("inline_keyboard", [])
              + filas}
    if uhttp._live_edit(cfg, ph, texto, markup=kb):
        razonamiento.log_teclado("cotb n=%d" % n, ph, kb)
        return None  # edited in place: nothing new below
    return r


def _cmd_last(cfg, rest):
    try:
        with open(os.path.join(config.STATE_DIR, config.ARCH_ULTIMA_RESPUESTA),
                  errors="replace") as f:
            texto = f.read()
    except OSError:
        return "🤷 No hay respuesta previa guardada."
    if not texto.strip():
        return "🤷 La última respuesta estaba vacía."
    _send_long_reply(cfg, "respuesta completa", 0, texto, max_partes=10)
    return None


def _cmd_cot(cfg, rest):
    """/cot [n|@sid] — step by step + full reasoning of a task as a
    scrollable document. No args: the active one. Also works for tasks
    run in the Mac app (reading history DOES cross runtimes — what does
    not cross is stopping)."""
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
            with open(os.path.join(config.STATE_DIR, config.ARCH_ULTIMO_TRACE), "w") as f:
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
            "/diff — trabajo en curso (botón por archivo) · /diff log: progreso · /diff commit\n"
            "/queue — contenido de las colas (⏫ lanzar / 🗑 descartar)\n"
            "/retry — relanza el último prompt (tras fallo o crash)\n"
            "/stop — frena tu corrida del celular y LIMPIA su cola de mensajes\n"
            "/nuclear — ☢️ emergencia: frena TODO (celular + Mac) y reinicia\n"
            "/deploy — 🚀 corre el pipeline (tests→backup→recarga) con botón\n"
            "/compact — lista Projects para compactar · /all_compact todas\n"
            "(permisos 🔐 y preguntas ❓ se responden tocando los botones, o con texto/número)")
