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
from zcode_bot.core import active_bot_session
from zcode_bot.reporting import (build_trace, _send_long_reply, quota_report,
                                 usage_report, context_report,
                                 compact_list_report, _compact_args,
                                 status_report, _tasks_scope, _ctx_tasks)
from zcode_bot.messages import (_fetch_messages, _extract_assistant_text,
                                _steps_of_messages)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.logging import log


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
        ("diff", "último commit del workspace activo · /diff dirty = sin commit"),
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


# --------------------------------------------------------------- F38: /diff
_DIFF_PATCH_CAP = 2600  # chars por PARTE del patch, bajo CH_CAJA
_DIFF_ASUNTO_CAP = 160  # asunto del commit (los de la casa son párrafos)
_DIFF_BOTONES_MAX = 20  # un botón por archivo; más que eso se lista sin botón
_GIT_TIMEOUT_S = 15


def _git(ws, *args):
    import subprocess
    r = subprocess.run(["git", "-C", ws] + list(args),
                       capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:160] or "git falló")
    return r.stdout


def _diff_archivos(ws, dirty):
    """[(path, +a, -b)] en el orden estable de git (numstat); binarios '-'."""
    out = _git(ws, *(["diff", "--numstat"] if dirty
                     else ["show", "--numstat", "--format=", "HEAD"]))
    archivos = []
    for line in out.splitlines():
        partes = line.split("\t")
        if len(partes) == 3:
            archivos.append((partes[2], partes[0], partes[1]))
    return archivos


def _diff_partes(patch):
    """F38c (idea de Damian): un patch grande se parte en N partes IGUALES
    por líneas (N = techo(len/CAP), objetivo = len/N) para verlo completo
    por partes con botones — nunca se recorta. Una línea más larga que el
    objetivo se corta en duro (patches de minificados)."""
    if len(patch) <= _DIFF_PATCH_CAP:
        return [patch]
    n = -(-len(patch) // _DIFF_PATCH_CAP)
    objetivo = -(-len(patch) // n)
    partes, actual = [], ""
    for linea in patch.splitlines(keepends=True):
        while len(linea) > objetivo:  # línea monstruo: en duro
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:objetivo])
            linea = linea[objetivo:]
        if len(actual) + len(linea) > objetivo and actual:
            partes.append(actual)
            actual = ""
        actual += linea
    if actual:
        partes.append(actual)
    # la granularidad por líneas deja un sobrante que caería en una parte
    # enana: se absorbe hacia atrás mientras quepa bajo el CAP
    while (len(partes) > n
           and len(partes[-1]) + len(partes[-2]) <= _DIFF_PATCH_CAP):
        partes[-2:] = [partes[-2] + partes[-1]]
    return partes


_DIFF_BOTONES_FILA = 5  # botones de partes por fila


def _botones_partes(n_arch, h, dirty, total, actual):
    filas, fila = [], []
    for p in range(1, total + 1):
        fila.append({"text": ("[%d/%d]" if p == actual else "%d/%d")
                     % (p, total),
                     "callback_data": ("dx:%d:%d" % (n_arch, p)) if dirty
                     else "df:%d:%s:%d" % (n_arch, h, p)})
        if len(fila) == _DIFF_BOTONES_FILA:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    return {"inline_keyboard": filas}


def _cmd_diff(cfg, rest):
    """/diff [dirty] [n [hash]] — qué cambió en el workspace de la task
    activa, desde el celu (F38b: idea de Damian — un BOTÓN por archivo).
    Nivel 1 (sin n): resumen del último commit (o de los cambios sin
    commit con `dirty`) — hash, asunto, hace cuánto, stat y la lista de
    archivos con +/−, cada uno con su botón; nunca se trunca. Nivel 2
    (n): el patch de ESE archivo. El hash corto viaja en el botón del
    commit: si HEAD se movió entre el resumen y el tap, se avisa en vez
    de mostrar otro archivo. Lectura local de git → sincrónico."""
    act = active_bot_session()
    ws = (act or {}).get("ws")
    if not ws or not os.path.isdir(ws):
        return "🎯 Sin task activa con workspace — /task n para elegir."
    tag = os.path.basename(ws)
    args = (rest or "").split()
    dirty = bool(args) and args[0].lower() == "dirty"
    if dirty:
        args = args[1:]
    try:
        _git(ws, "rev-parse", "--git-dir")
        h, asunto, cuando = _git(
            ws, "log", "-1", "--format=%h%x00%s%x00%cr").strip().split("\x00")
        archivos = _diff_archivos(ws, dirty)
        if args and args[0].isdigit():  # ---- nivel 2: un archivo [parte]
            n = int(args[0])
            resto = args[1:]
            # tras n: un hash corto (hex, >= 7) y/o una parte (entero chico)
            hash_btn = next((x for x in resto if len(x) >= 7), None)
            parte = next((int(x) for x in resto if x.isdigit() and len(x) < 4),
                         1)
            if not dirty and hash_btn and hash_btn != h:
                return ("🔄 El commit cambió (%s → %s) — mandá /diff de nuevo."
                        % (esc(hash_btn), esc(h)))
            if n < 1 or n > len(archivos):
                return ("⚠️ Archivo fuera de la lista (1 a %d) — mandá /diff."
                        % len(archivos))
            path, a, b = archivos[n - 1]
            patch = _git(ws, *(["diff", "--", path] if dirty
                               else ["show", "--format=", "HEAD", "--", path]))
            cab = "📄 <b>%s</b> · +%s/−%s · %s" % (
                esc(path), esc(a), esc(b),
                "sin commit" if dirty else "<code>%s</code>" % esc(h))
            if not patch.strip():
                return cab + "\n(sin patch)"
            partes = _diff_partes(patch)
            if len(partes) == 1:
                return cab + "\n<pre>%s</pre>" % esc(patch)
            parte = min(max(parte, 1), len(partes))
            cab += " · parte %d/%d" % (parte, len(partes))
            return (cab + "\n<pre>%s</pre>" % esc(partes[parte - 1]),
                    _botones_partes(n, h, dirty, len(partes), parte))
        # ---- nivel 1: resumen + un botón por archivo
        if dirty:
            stat = _git(ws, "diff", "--shortstat").strip() or "sin cambios"
            nuevos = [l for l in _git(ws, "status", "--porcelain").splitlines()
                      if l.startswith("??")]
            cab = ["📦 <b>diff</b> · 📁%s · cambios SIN commit" % esc(tag),
                   esc(stat)]
            if nuevos:
                s = "s" if len(nuevos) != 1 else ""
                cab.append("➕ %d archivo%s nuevo%s sin trackear (git add)"
                           % (len(nuevos), s, s))
        else:
            stat = _git(ws, "show", "--shortstat", "--format=", "HEAD").strip()
            sucios = [l for l in _git(ws, "status", "--porcelain").splitlines()
                      if l.strip()]
            cab = ["📦 <b>diff</b> · 📁%s · último commit" % esc(tag),
                   "<code>%s</code> «%s» · %s"
                   % (esc(h), esc(preview(asunto, _DIFF_ASUNTO_CAP)),
                      esc(cuando)),
                   esc(stat or "sin cambios")]
            if sucios:
                cab.append("✏️ %d archivo%s con cambios sin commit "
                           "(/diff dirty)" % (len(sucios),
                                              "s" if len(sucios) != 1 else ""))
    except Exception as e:  # noqa: BLE001 - git ausente/repo roto → mensaje
        return "❌ git en «%s»: %s" % (esc(tag), esc(str(e)[:150]))
    if not archivos:
        return "\n".join(cab) + "\n(sin archivos)"
    cab.append("Tocá un archivo para ver su patch:")
    kb = []
    for i, (path, a, b) in enumerate(archivos, 1):
        cab.append("  %d. %s +%s/−%s" % (i, esc(path), esc(a), esc(b)))
        if i <= _DIFF_BOTONES_MAX:
            kb.append([{"text": ("%d. %s" % (i, os.path.basename(path)))[:36],
                        "callback_data": ("dx:%d" % i) if dirty
                        else "df:%d:%s" % (i, h)}])
    if len(archivos) > _DIFF_BOTONES_MAX:
        cab.append("(botones para los primeros %d; el resto con /diff%s n)"
                   % (_DIFF_BOTONES_MAX, " dirty" if dirty else ""))
    return "\n".join(cab), {"inline_keyboard": kb}



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
            "/diff — último commit del workspace activo · /diff dirty: sin commit\n"
            "/queue — contenido de las colas (⏫ lanzar / 🗑 descartar)\n"
            "/retry — relanza el último prompt (tras fallo o crash)\n"
            "/stop — frena tu corrida del celular y LIMPIA su cola de mensajes\n"
            "/nuclear — ☢️ emergencia: frena TODO (celular + Mac) y reinicia\n"
            "/deploy — 🚀 corre el pipeline (tests→backup→recarga) con botón\n"
            "/compact — lista Projects para compactar · /all_compact todas\n"
            "(permisos 🔐 y preguntas ❓ se responden tocando los botones, o con texto/número)")
