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


# --------------------------------------------------------------- F38: /diff
_DIFF_PATCH_CAP = 2600  # chars por PARTE del patch, bajo CH_CAJA
_DIFF_ASUNTO_CAP = 160  # asunto del commit (los de la casa son párrafos)
_DIFF_BOTONES_MAX = 20  # un botón por archivo; más que eso se lista sin botón
_DIFF_BOTONES_FILA = 5  # botones de partes por fila
_DIFF_LOG_N = 10        # commits del historial (/diff log)
_DIFF_LOG_ASUNTO = 70   # asunto en la lista del historial
_GIT_TIMEOUT_S = 15


def _git(ws, *args, ok_rc=(0,)):
    import subprocess
    r = subprocess.run(["git", "-C", ws] + list(args),
                       capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    if r.returncode not in ok_rc:
        raise RuntimeError((r.stderr or r.stdout).strip()[:160] or "git falló")
    return r.stdout


def _commit_info(ws, ref):
    """(hash corto, asunto, hace cuánto) de un commit."""
    return _git(ws, "log", "-1", "--format=%h%x00%s%x00%cr",
                ref).strip().split("\x00")


def _diff_archivos(ws, ref):
    """[(path, +a, -b, nuevo)] en el orden estable de git (numstat).
    ref=None → TRABAJO EN CURSO: árbol vs HEAD (staged + unstaged) y al
    final los nuevos sin trackear (F38e — Damian: '¿el diff es del último
    commit? ¿y el trabajo en curso?'). Binarios: '-'."""
    out = (_git(ws, "diff", "HEAD", "--numstat") if ref is None
           else _git(ws, "show", "--numstat", "--format=", ref))
    archivos = []
    for line in out.splitlines():
        partes = line.split("\t")
        if len(partes) == 3:
            archivos.append((partes[2], partes[0], partes[1], False))
    if ref is None:
        for ln in _git(ws, "status", "--porcelain", "-uall").splitlines():
            if ln.startswith("??"):
                path = ln[3:].strip()
                try:
                    with open(os.path.join(ws, path), errors="replace") as f:
                        n = sum(1 for _ in f)
                except OSError:
                    n = 0
                archivos.append((path, str(n), "0", True))
    return archivos


def _diff_patch(ws, ref, path, nuevo):
    if nuevo:  # sin trackear: todo agregado (rc 1 = hay diferencias)
        return _git(ws, "diff", "--no-index", "--", "/dev/null", path,
                    ok_rc=(0, 1))
    if ref is None:
        return _git(ws, "diff", "HEAD", "--", path)
    return _git(ws, "show", "--format=", ref, "--", path)


def _diff_coloreado_git(patch):
    """F38d: salida de git show/diff de UN archivo → el mismo coloreado que
    la caja viva (colorear_diff). Todo lo anterior al primer @@ es
    encabezado de git (diff --git / index / --- / +++ / modos): se tira,
    el nombre ya va en el título 📄. None si no hay hunks (binario)."""
    lineas = patch.splitlines()
    i = next((k for k, ln in enumerate(lineas) if ln.startswith("@@")), None)
    if i is None:
        return None
    return "\n".join(colorear_diff(lineas[i:], hunks_sep=True)) + "\n"


def _diff_partes(patch):
    """F38c: partes iguales del patch coloreado (splitter compartido)."""
    return partes_iguales(patch, _DIFF_PATCH_CAP)


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


def _botones_vistas(ref):
    """Fila para saltar entre las vistas (en curso ↔ commit ↔ historial)."""
    fila = []
    if ref is None:
        fila.append({"text": "📦 último commit", "callback_data": "dc:HEAD"})
    else:
        fila.append({"text": "✏️ en curso", "callback_data": "dw"})
    fila.append({"text": "🕘 historial", "callback_data": "dl"})
    return fila


def _diff_log(ws, tag):
    """/diff log — el PROGRESO: los últimos commits, un botón cada uno."""
    out = _git(ws, "log", "-%d" % _DIFF_LOG_N, "--format=%h%x00%s%x00%cr")
    lines = ["🕘 <b>historial</b> · 📁%s · últimos commits" % esc(tag)]
    kb = []
    for i, ln in enumerate(out.splitlines(), 1):
        h, s, cuando = ln.split("\x00")
        lines.append("  %d. <code>%s</code> «%s» · %s"
                     % (i, esc(h), esc(preview(s, _DIFF_LOG_ASUNTO)),
                        esc(cuando)))
        kb.append([{"text": ("%s %s" % (h, s))[:36],
                    "callback_data": "dc:%s" % h}])
    kb.append([{"text": "✏️ en curso", "callback_data": "dw"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def _diff_vista(ws, tag, ref, resto, nota=""):
    """Nivel 1 (resumen + un botón por archivo) o nivel 2 (un archivo
    [parte]) de una fuente: ref=None es el trabajo en curso, si no un
    commit. El hash corto viaja en los botones del commit (df:n:hash): un
    botón viejo abre SU commit, no el HEAD de ahora."""
    archivos = _diff_archivos(ws, ref)
    h = None if ref is None else _commit_info(ws, ref)[0]
    if resto and resto[0].isdigit():  # ---- nivel 2: un archivo [parte]
        n = int(resto[0])
        parte = (int(resto[1]) if len(resto) > 1 and resto[1].isdigit()
                 else 1)
        if n < 1 or n > len(archivos):
            return ("⚠️ Archivo fuera de la lista (1 a %d) — mandá /diff."
                    % len(archivos))
        path, a, b, nuevo = archivos[n - 1]
        patch = _diff_patch(ws, ref, path, nuevo)
        fuente = (("en curso" + (" · ➕ nuevo" if nuevo else ""))
                  if ref is None else "<code>%s</code>" % esc(h))
        cab = "📄 <b>%s</b> · +%s/−%s · %s" % (esc(path), esc(a), esc(b),
                                               fuente)
        texto = _diff_coloreado_git(patch) if patch.strip() else None
        if texto is None:
            return cab + ("\n(archivo binario)" if "Binary" in patch
                          else "\n(sin patch)")
        partes = _diff_partes(texto)
        if len(partes) == 1:
            return cab + "\n<pre>%s</pre>" % esc(texto)
        parte = min(max(parte, 1), len(partes))
        cab += " · parte %d/%d" % (parte, len(partes))
        return (cab + "\n<pre>%s</pre>" % esc(partes[parte - 1]),
                _botones_partes(n, h, ref is None, len(partes), parte))
    # ---- nivel 1: resumen + un botón por archivo
    if ref is None:
        stat = (_git(ws, "diff", "HEAD", "--shortstat").strip()
                or "sin cambios en archivos trackeados")
        nuevos = sum(1 for x in archivos if x[3])
        cab = ["📦 <b>diff</b> · 📁%s · ✏️ trabajo en curso (sin commit)"
               % esc(tag), esc(stat)]
        if nuevos:
            s = "s" if nuevos != 1 else ""
            cab.append("➕ %d archivo%s nuevo%s sin trackear" % (nuevos, s, s))
    else:
        _h, asunto, cuando = _commit_info(ws, ref)
        stat = _git(ws, "show", "--shortstat", "--format=", ref).strip()
        cab = ["📦 <b>diff</b> · 📁%s · 📦 commit" % esc(tag),
               "<code>%s</code> «%s» · %s"
               % (esc(h), esc(preview(asunto, _DIFF_ASUNTO_CAP)),
                  esc(cuando)),
               esc(stat or "sin cambios")]
    if nota:
        cab.insert(1, nota)
    kb = []
    if archivos:
        cab.append("Tocá un archivo para ver su patch:")
        for i, (path, a, b, nuevo) in enumerate(archivos, 1):
            cab.append("  %d. %s%s +%s/−%s" % (i, "➕ " if nuevo else "",
                                                esc(path), esc(a), esc(b)))
            if i <= _DIFF_BOTONES_MAX:
                kb.append([{"text": ("%d. %s"
                                     % (i, os.path.basename(path)))[:36],
                            "callback_data": ("dx:%d" % i) if ref is None
                            else "df:%d:%s" % (i, h)}])
        if len(archivos) > _DIFF_BOTONES_MAX:
            cab.append("(botones para los primeros %d; el resto con /diff%s n)"
                       % (_DIFF_BOTONES_MAX,
                          " dirty" if ref is None else " commit %s" % h))
    else:
        cab.append("(sin archivos)")
    kb.append(_botones_vistas(ref))
    return "\n".join(cab), {"inline_keyboard": kb}


def _cmd_diff(cfg, rest):
    """/diff — qué cambió en el workspace de la task activa, desde el celu
    (F38b/c/d/e, ideas de Damian: un botón por archivo, partes iguales,
    los globitos de la caja viva, y el TRABAJO EN CURSO como default).
      /diff                 trabajo en curso (árbol vs HEAD + nuevos);
                            árbol limpio → el último commit, avisando
      /diff commit [hash]   un commit (default: el último)
      /diff log             el progreso: últimos commits con botones
      /diff dirty n [p]     archivo n del trabajo en curso [parte p]
      /diff n hash [p]      archivo n del commit hash [parte p]
    Lectura local de git → sincrónico (no SLOW_CMDS)."""
    act = active_bot_session()
    ws = (act or {}).get("ws")
    if not ws or not os.path.isdir(ws):
        return "🎯 Sin task activa con workspace — /task n para elegir."
    tag = os.path.basename(ws)
    args = (rest or "").split()
    modo = args[0].lower() if args else ""
    try:
        _git(ws, "rev-parse", "--git-dir")
        if modo == "log":
            return _diff_log(ws, tag)
        if modo == "dirty":
            return _diff_vista(ws, tag, None, args[1:])
        if modo == "commit":
            return _diff_vista(ws, tag, args[1] if len(args) > 1 else "HEAD",
                               args[2:])
        if args and args[0].isdigit():  # botón df:n:hash[:p] (o a mano)
            resto = args[1:]
            hash_btn = next((x for x in resto if len(x) >= 7), None)
            return _diff_vista(ws, tag, hash_btn or "HEAD",
                               [args[0]] + [x for x in resto if x != hash_btn])
        if _git(ws, "status", "--porcelain", "-uall").strip():
            return _diff_vista(ws, tag, None, [])
        return _diff_vista(ws, tag, "HEAD", [],
                           nota="🧼 árbol limpio — te muestro el último commit")
    except Exception as e:  # noqa: BLE001 - git ausente/repo roto → mensaje
        return "❌ git en «%s»: %s" % (esc(tag), esc(str(e)[:150]))


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
