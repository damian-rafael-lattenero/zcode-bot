"""commands.handlers_diff — the /diff domain (R8, F38b/c/d/e).

Moved VERBATIM from handlers_info (F9 pattern): what changed in the
active task's workspace, from the phone — one button per file, equal
parts of the patch, the shared coloring of the live box and WORK IN
PROGRESS as the default. Damian's ideas throughout F38. handlers_info
re-exports _cmd_diff (dispatch and tests import it from there).
"""
import os

from zcode_bot.core import active_bot_session
from zcode_bot.messages import colorize_diff
from zcode_bot.utils.formatting import (esc, preview, equal_parts,
                                          lines_stamp)


# --------------------------------------------------------------- F38: /diff
_DIFF_PATCH_CAP = 2600  # chars per patch PART, below CH_BOX
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


def _file_line_count(ws, path):
    """F50b — line count of a workspace file (None if it cannot be read:
    deleted/binary/odd encoding)."""
    try:
        with open(os.path.join(ws, path), errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def _diff_files(ws, ref):
    """[(path, +a, -b, new)] in git's stable order (numstat).
    ref=None → WORK IN PROGRESS: tree vs HEAD (staged + unstaged) and at
    the end the untracked new files (F38e — Damian: 'is the diff from
    the last commit? what about work in progress?'). Binaries: '-'."""
    out = (_git(ws, "diff", "HEAD", "--numstat") if ref is None
           else _git(ws, "show", "--numstat", "--format=", ref))
    files = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            files.append((fields[2], fields[0], fields[1], False))
    if ref is None:
        for ln in _git(ws, "status", "--porcelain", "-uall").splitlines():
            if ln.startswith("??"):
                path = ln[3:].strip()
                n = _file_line_count(ws, path)
                files.append((path, str(n if n is not None else 0),
                              "0", True))
    return files


def _diff_patch(ws, ref, path, is_new):
    if is_new:  # untracked: all added (rc 1 = there are differences)
        return _git(ws, "diff", "--no-index", "--", "/dev/null", path,
                    ok_rc=(0, 1))
    if ref is None:
        return _git(ws, "diff", "HEAD", "--", path)
    return _git(ws, "show", "--format=", ref, "--", path)


def _git_diff_colored(patch):
    """F38d: git show/diff output of ONE file → the same coloring as the
    live box (colorize_diff). Everything before the first @@ is git
    header (diff --git / index / --- / +++ / modes): dropped, the name
    is already in the 📄 title. None if there are no hunks (binary).
    F55b: colorize_diff numbers each line from the @@ headers (the NEW
    file's number; removed lines carry the OLD file's)."""
    lines = patch.splitlines()
    i = next((k for k, ln in enumerate(lines) if ln.startswith("@@")), None)
    if i is None:
        return None
    return "\n".join(colorize_diff(lines[i:])) + "\n"


def _diff_parts(patch):
    """F38c: equal parts of the colored patch (shared splitter)."""
    return equal_parts(patch, _DIFF_PATCH_CAP)


def _part_buttons(n_files, h, dirty, total, current):
    rows, row = [], []
    for p in range(1, total + 1):
        row.append({"text": ("[%d/%d]" if p == current else "%d/%d")
                    % (p, total),
                    "callback_data": ("dx:%d:%d" % (n_files, p)) if dirty
                    else "df:%d:%s:%d" % (n_files, h, p)})
        if len(row) == _DIFF_BOTONES_FILA:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def _view_buttons(ref):
    """Row to jump between the views (in progress ↔ commit ↔ history)."""
    row = []
    if ref is None:
        row.append({"text": "📦 last commit", "callback_data": "dc:HEAD"})
    else:
        row.append({"text": "✏️ in progress", "callback_data": "dw"})
    row.append({"text": "🕘 history", "callback_data": "dl"})
    return row


def _diff_log(ws, tag):
    """/diff log — the PROGRESS: the latest commits, one button each."""
    out = _git(ws, "log", "-%d" % _DIFF_LOG_N, "--format=%h%x00%s%x00%cr")
    lines = ["🕘 <b>history</b> · 📁%s · latest commits" % esc(tag)]
    kb = []
    for i, ln in enumerate(out.splitlines(), 1):
        h, s, ago = ln.split("\x00")
        lines.append("  %d. <code>%s</code> «%s» · %s"
                     % (i, esc(h), esc(preview(s, _DIFF_LOG_ASUNTO)),
                        esc(ago)))
        kb.append([{"text": ("%s %s" % (h, s))[:36],
                    "callback_data": "dc:%s" % h}])
    kb.append([{"text": "✏️ in progress", "callback_data": "dw"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def _diff_view(ws, tag, ref, rest, note=""):
    """Level 1 (summary + one button per file) or level 2 (one file
    [part]) of a source: ref=None is work in progress, otherwise a
    commit. The short hash travels in the commit's buttons (df:n:hash):
    an old button opens ITS commit, not the current HEAD."""
    files = _diff_files(ws, ref)
    h = None if ref is None else _commit_info(ws, ref)[0]
    if rest and rest[0].isdigit():  # ---- level 2: one file [part]
        n = int(rest[0])
        part = (int(rest[1]) if len(rest) > 1 and rest[1].isdigit()
                else 1)
        if n < 1 or n > len(files):
            return ("⚠️ File out of the list (1 to %d) — send /diff."
                    % len(files))
        path, a, b, is_new = files[n - 1]
        patch = _diff_patch(ws, ref, path, is_new)
        source = (("in progress" + (" · ➕ new" if is_new else ""))
                  if ref is None else "<code>%s</code>" % esc(h))
        # F50b: the line stamp of ALL files — diff dots (🟢/🔴) and the
        # file's lines, with or without a patch
        head = "📄 <b>%s</b> · %s · %s" % (
            esc(path),
            lines_stamp(a, b, _file_line_count(ws, path)), source)
        text = _git_diff_colored(patch) if patch.strip() else None
        if text is None:
            return head + ("\n(binary file)" if "Binary" in patch
                           else "\n(no patch)")
        parts = _diff_parts(text)
        if len(parts) == 1:
            return head + "\n<pre>%s</pre>" % esc(text)
        part = min(max(part, 1), len(parts))
        head += " · part %d/%d" % (part, len(parts))
        return (head + "\n<pre>%s</pre>" % esc(parts[part - 1]),
                _part_buttons(n, h, ref is None, len(parts), part))
    # ---- level 1: summary + one button per file
    if ref is None:
        stat = (_git(ws, "diff", "HEAD", "--shortstat").strip()
                or "no changes in tracked files")
        new_count = sum(1 for x in files if x[3])
        head = ["📦 <b>diff</b> · 📁%s · ✏️ work in progress (no commit)"
                % esc(tag), esc(stat)]
        if new_count:
            s = "s" if new_count != 1 else ""
            head.append("➕ %d new untracked file%s" % (new_count, s))
    else:
        _h, subject, ago = _commit_info(ws, ref)
        stat = _git(ws, "show", "--shortstat", "--format=", ref).strip()
        head = ["📦 <b>diff</b> · 📁%s · 📦 commit" % esc(tag),
                "<code>%s</code> «%s» · %s"
                % (esc(h), esc(preview(subject, _DIFF_ASUNTO_CAP)),
                   esc(ago)),
                esc(stat or "no changes")]
    if note:
        head.insert(1, note)
    kb = []
    if files:
        head.append("Tap a file to see its patch:")
        for i, (path, a, b, is_new) in enumerate(files, 1):
            # F50b: dots + file lines in the listing too
            head.append("  %d. %s%s %s" % (
                i, "➕ " if is_new else "", esc(path),
                lines_stamp(a, b, _file_line_count(ws, path))))
            if i <= _DIFF_BOTONES_MAX:
                kb.append([{"text": ("%d. %s"
                                     % (i, os.path.basename(path)))[:36],
                            "callback_data": ("dx:%d" % i) if ref is None
                            else "df:%d:%s" % (i, h)}])
        if len(files) > _DIFF_BOTONES_MAX:
            head.append("(buttons for the first %d; the rest with /diff%s n)"
                        % (_DIFF_BOTONES_MAX,
                           " dirty" if ref is None else " commit %s" % h))
    else:
        head.append("(no files)")
    kb.append(_view_buttons(ref))
    return "\n".join(head), {"inline_keyboard": kb}


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
    mode = args[0].lower() if args else ""
    try:
        _git(ws, "rev-parse", "--git-dir")
        if mode == "log":
            return _diff_log(ws, tag)
        if mode == "dirty":
            return _diff_view(ws, tag, None, args[1:])
        if mode == "commit":
            return _diff_view(ws, tag, args[1] if len(args) > 1 else "HEAD",
                              args[2:])
        if args and args[0].isdigit():  # df:n:hash[:p] button (or manual)
            tail = args[1:]
            hash_btn = next((x for x in tail if len(x) >= 7), None)
            return _diff_view(ws, tag, hash_btn or "HEAD",
                              [args[0]] + [x for x in tail if x != hash_btn])
        if _git(ws, "status", "--porcelain", "-uall").strip():
            return _diff_view(ws, tag, None, [])
        return _diff_view(ws, tag, "HEAD", [],
                          note="🧼 clean tree — showing you the last commit")
    except Exception as e:  # noqa: BLE001 - git missing/broken repo → message
        return "❌ git in «%s»: %s" % (esc(tag), esc(str(e)[:150]))
