"""commands.handlers_diff — the /diff domain (R8, F38b/c/d/e).

Moved VERBATIM from handlers_info (F9 pattern): what changed in the
active task's workspace, from the phone — one button per file, equal
parts of the patch, the shared coloring of the live box and WORK IN
PROGRESS as the default. Damian's ideas throughout F38. handlers_info
re-exports _cmd_diff (dispatch and tests import it from there).
"""
import os

from zcode_bot.core import active_bot_session
from zcode_bot.messages import colorear_diff
from zcode_bot.utils.formatting import (esc, preview, partes_iguales,
                                          sello_lineas)


# --------------------------------------------------------------- F38: /diff
_DIFF_PATCH_CAP = 2600  # chars per patch PART, below CH_CAJA
_DIFF_ASUNTO_CAP = 160  # commit subject (ours are paragraphs)
_DIFF_BOTONES_MAX = 20  # one button per file; beyond that, listed without a button
_DIFF_BOTONES_FILA = 5  # part buttons per row
_DIFF_LOG_N = 10        # commits in the history (/diff log)
_DIFF_LOG_ASUNTO = 70   # subject in the history list
_GIT_TIMEOUT_S = 15


def _git(ws, *args, ok_rc=(0,)):
    import subprocess
    r = subprocess.run(["git", "-C", ws] + list(args),
                       capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    if r.returncode not in ok_rc:
        raise RuntimeError((r.stderr or r.stdout).strip()[:160] or "git failed")
    return r.stdout


def _commit_info(ws, ref):
    """(short hash, subject, how long ago) of a commit."""
    return _git(ws, "log", "-1", "--format=%h%x00%s%x00%cr",
                ref).strip().split("\x00")


def _n_lineas_archivo(ws, path):
    """F50b — line count of a workspace file (None if it cannot be read:
    deleted/binary/odd encoding)."""
    try:
        with open(os.path.join(ws, path), errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def _diff_archivos(ws, ref):
    """[(path, +a, -b, new)] in git's stable order (numstat).
    ref=None → WORK IN PROGRESS: tree vs HEAD (staged + unstaged) and at
    the end the untracked new files (F38e — Damian: 'is the diff from
    the last commit? what about work in progress?'). Binaries: '-'."""
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
                n = _n_lineas_archivo(ws, path)
                archivos.append((path, str(n if n is not None else 0),
                                 "0", True))
    return archivos


def _diff_patch(ws, ref, path, nuevo):
    if nuevo:  # untracked: all added (rc 1 = there are differences)
        return _git(ws, "diff", "--no-index", "--", "/dev/null", path,
                    ok_rc=(0, 1))
    if ref is None:
        return _git(ws, "diff", "HEAD", "--", path)
    return _git(ws, "show", "--format=", ref, "--", path)


def _diff_coloreado_git(patch):
    """F38d: git show/diff output of ONE file → the same coloring as the
    live box (colorear_diff). Everything before the first @@ is git
    header (diff --git / index / --- / +++ / modes): dropped, the name
    is already in the 📄 title. None if there are no hunks (binary)."""
    lineas = patch.splitlines()
    i = next((k for k, ln in enumerate(lineas) if ln.startswith("@@")), None)
    if i is None:
        return None
    return "\n".join(colorear_diff(lineas[i:], hunks_sep=True)) + "\n"


def _diff_partes(patch):
    """F38c: equal parts of the colored patch (shared splitter)."""
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
    """Row to jump between the views (in progress ↔ commit ↔ history)."""
    fila = []
    if ref is None:
        fila.append({"text": "📦 last commit", "callback_data": "dc:HEAD"})
    else:
        fila.append({"text": "✏️ in progress", "callback_data": "dw"})
    fila.append({"text": "🕘 history", "callback_data": "dl"})
    return fila


def _diff_log(ws, tag):
    """/diff log — the PROGRESS: the latest commits, one button each."""
    out = _git(ws, "log", "-%d" % _DIFF_LOG_N, "--format=%h%x00%s%x00%cr")
    lines = ["🕘 <b>history</b> · 📁%s · latest commits" % esc(tag)]
    kb = []
    for i, ln in enumerate(out.splitlines(), 1):
        h, s, cuando = ln.split("\x00")
        lines.append("  %d. <code>%s</code> «%s» · %s"
                     % (i, esc(h), esc(preview(s, _DIFF_LOG_ASUNTO)),
                        esc(cuando)))
        kb.append([{"text": ("%s %s" % (h, s))[:36],
                    "callback_data": "dc:%s" % h}])
    kb.append([{"text": "✏️ in progress", "callback_data": "dw"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def _diff_vista(ws, tag, ref, resto, nota=""):
    """Level 1 (summary + one button per file) or level 2 (one file
    [part]) of a source: ref=None is work in progress, otherwise a
    commit. The short hash travels in the commit's buttons (df:n:hash):
    an old button opens ITS commit, not the current HEAD."""
    archivos = _diff_archivos(ws, ref)
    h = None if ref is None else _commit_info(ws, ref)[0]
    if resto and resto[0].isdigit():  # ---- level 2: one file [part]
        n = int(resto[0])
        parte = (int(resto[1]) if len(resto) > 1 and resto[1].isdigit()
                 else 1)
        if n < 1 or n > len(archivos):
            return ("⚠️ File out of the list (1 to %d) — send /diff."
                    % len(archivos))
        path, a, b, nuevo = archivos[n - 1]
        patch = _diff_patch(ws, ref, path, nuevo)
        fuente = (("in progress" + (" · ➕ new" if nuevo else ""))
                  if ref is None else "<code>%s</code>" % esc(h))
        # F50b: the line stamp of ALL files — diff dots (🟢/🔴) and the
        # file's lines, with or without a patch
        cab = "📄 <b>%s</b> · %s · %s" % (
            esc(path),
            sello_lineas(a, b, _n_lineas_archivo(ws, path)), fuente)
        texto = _diff_coloreado_git(patch) if patch.strip() else None
        if texto is None:
            return cab + ("\n(binary file)" if "Binary" in patch
                          else "\n(no patch)")
        partes = _diff_partes(texto)
        if len(partes) == 1:
            return cab + "\n<pre>%s</pre>" % esc(texto)
        parte = min(max(parte, 1), len(partes))
        cab += " · part %d/%d" % (parte, len(partes))
        return (cab + "\n<pre>%s</pre>" % esc(partes[parte - 1]),
                _botones_partes(n, h, ref is None, len(partes), parte))
    # ---- level 1: summary + one button per file
    if ref is None:
        stat = (_git(ws, "diff", "HEAD", "--shortstat").strip()
                or "no changes in tracked files")
        nuevos = sum(1 for x in archivos if x[3])
        cab = ["📦 <b>diff</b> · 📁%s · ✏️ work in progress (no commit)"
               % esc(tag), esc(stat)]
        if nuevos:
            s = "s" if nuevos != 1 else ""
            cab.append("➕ %d new untracked file%s" % (nuevos, s))
    else:
        _h, asunto, cuando = _commit_info(ws, ref)
        stat = _git(ws, "show", "--shortstat", "--format=", ref).strip()
        cab = ["📦 <b>diff</b> · 📁%s · 📦 commit" % esc(tag),
               "<code>%s</code> «%s» · %s"
               % (esc(h), esc(preview(asunto, _DIFF_ASUNTO_CAP)),
                  esc(cuando)),
               esc(stat or "no changes")]
    if nota:
        cab.insert(1, nota)
    kb = []
    if archivos:
        cab.append("Tap a file to see its patch:")
        for i, (path, a, b, nuevo) in enumerate(archivos, 1):
            # F50b: dots + file lines in the listing too
            cab.append("  %d. %s%s %s" % (
                i, "➕ " if nuevo else "", esc(path),
                sello_lineas(a, b, _n_lineas_archivo(ws, path))))
            if i <= _DIFF_BOTONES_MAX:
                kb.append([{"text": ("%d. %s"
                                     % (i, os.path.basename(path)))[:36],
                            "callback_data": ("dx:%d" % i) if ref is None
                            else "df:%d:%s" % (i, h)}])
        if len(archivos) > _DIFF_BOTONES_MAX:
            cab.append("(buttons for the first %d; the rest with /diff%s n)"
                       % (_DIFF_BOTONES_MAX,
                          " dirty" if ref is None else " commit %s" % h))
    else:
        cab.append("(no files)")
    kb.append(_botones_vistas(ref))
    return "\n".join(cab), {"inline_keyboard": kb}


def _cmd_diff(cfg, rest):
    """/diff — what changed in the active task's workspace, from the
    phone (F38b/c/d/e, Damian's ideas: one button per file, equal
    parts, the little bubbles of the live box, and WORK IN PROGRESS as
    the default).
      /diff                 work in progress (tree vs HEAD + new files);
                            clean tree → the last commit, with a notice
      /diff commit [hash]   one commit (default: the last one)
      /diff log             the progress: latest commits with buttons
      /diff dirty n [p]     file n of the work in progress [part p]
      /diff n hash [p]      file n of commit hash [part p]
    Local git read → synchronous (not in SLOW_CMDS)."""
    act = active_bot_session()
    ws = (act or {}).get("ws")
    if not ws or not os.path.isdir(ws):
        return "🎯 No active task with a workspace — /task n to pick one."
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
        if args and args[0].isdigit():  # df:n:hash[:p] button (or manual)
            resto = args[1:]
            hash_btn = next((x for x in resto if len(x) >= 7), None)
            return _diff_vista(ws, tag, hash_btn or "HEAD",
                               [args[0]] + [x for x in resto if x != hash_btn])
        if _git(ws, "status", "--porcelain", "-uall").strip():
            return _diff_vista(ws, tag, None, [])
        return _diff_vista(ws, tag, "HEAD", [],
                           nota="🧼 clean tree — showing you the last commit")
    except Exception as e:  # noqa: BLE001 - git missing/broken repo → message
        return "❌ git in «%s»: %s" % (esc(tag), esc(str(e)[:150]))
