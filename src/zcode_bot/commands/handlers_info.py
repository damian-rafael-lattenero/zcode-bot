"""commands.handlers_info — handlers de informes y menú (F9).

Movido TEXTUAL de commands.dispatch (deuda declarada en ADR-012, cierre
F9): register_bot_menu, _cmd_quota, _cmd_usage, _cmd_ctx, _cmd_compact,
_cmd_all_compact, _cmd_status, _cmd_last, _cmd_cot y _cmd_help. Son los
comandos de LECTURA: todo el cuerpo pesado vive en reporting, acá solo
se adapta la respuesta al chat.
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
    """Menú de comandos de Telegram (botón '/'): se re-registra al arrancar
    el poller para mantenerlo al día. R6: la lista se DERIVA de las specs
    de dispatch (un solo lugar declara comando+descripción; los visibles
    van en el orden de la tabla)."""
    from zcode_bot.commands.dispatch import SPECS  # tardío: dispatch nos importa
    cmds = [(s.names[0], s.menu_desc) for s in SPECS if s.menu_desc]
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
    # F38: comandos lentos en vuelo — el snapshot vive en dispatch, pero
    # dispatch importa ESTE módulo (tabla REG): import tardío, sin ciclo
    from zcode_bot.commands import dispatch as zc_dispatch
    return status_report(inflight=zc_dispatch.inflight_snapshot())


# R8: /diff vive en handlers_diff (movimiento textual); re-exportado
# porque dispatch y los tests lo importan de acá.
from zcode_bot.commands.handlers_diff import _cmd_diff  # noqa: E402,F401


def _cmd_cotb(cfg, rest):
    """/cotb <sid> <t0> <n> [parte] — lo disparan los botones «CoT R.C»
    (ct:sid:t0:n[:p]). Lee el cache: instantáneo, sin runtime. F43c (idea
    de Damian): el contenido se muestra EN el mensaje de las cajitas —
    edita ese mensaje con el texto de la caja y el teclado intacto para
    seguir navegando; abajo no cae nada nuevo. F46: con phs (una caja de
    botones POR RAZONADA) edita el mensaje de SU razonada; sin phs (cajas
    viejas) o si el edit falla, el ph único de siempre o mensaje aparte."""
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
    ph = (phs[n - 1] if len(phs) >= n and phs[n - 1] else None) \
        or d.get("ph")
    if not ph:
        return r
    texto, kb = r if isinstance(r, tuple) else (r, None)
    if kb is None:  # teclado de cajas completo: seguir navegando en el lugar
        rotulos = d.get("rotulos") or []
        if rotulos:
            kb = {"inline_keyboard": razonamiento.botones_cajas(sid, t0,
                                                                rotulos)}
    if uhttp._live_edit(cfg, ph, texto, markup=kb):
        return None  # editado en el lugar: nada nuevo abajo
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
