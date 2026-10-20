"""commands.handlers_diff — el dominio de /diff (R8, F38b/c/d/e).

Movido TEXTUAL de handlers_info (patrón F9): qué cambió en el workspace
de la task activa, desde el celu — un botón por archivo, partes iguales
del patch, el coloreado compartido de la caja viva y el TRABAJO EN CURSO
como default. Ideas de Damian a lo largo de F38. handlers_info re-exporta
_cmd_diff (dispatch y tests lo importan de ahí).
"""
import os

from zcode_bot.core import active_bot_session
from zcode_bot.messages import colorear_diff
from zcode_bot.utils.formatting import esc, preview, partes_iguales


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
