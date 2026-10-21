"""Text formatting utilities for zcode_bot."""
import json
import re
import datetime
import os
import sqlite3

from zcode_bot import config
from .validation import first


def esc(s):
    """Escapes HTML for Telegram (parse_mode=HTML). Single source: the
    gateway imported this broken copy (entities lost in a copy-paste)
    while using its local def — consolidated 15/10."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def preview(value, limit=280):
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def fmt_tok(n):
    """15800 -> '15.8K' · 17400000 -> '17.4M'"""
    if n is None:
        return "?"
    n = float(n)
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}K"
    return str(int(n))


def fmt_num(n):
    """For the panel's credits: 880 -> '880' · 28830 -> '28.8K' (K only from 10K up)"""
    if n is None:
        return "?"
    n = float(n)
    if n >= 1e4:
        v = n / 1e3
        return ("%.1fK" % v).replace(".0K", "K")
    return str(int(n))


def datetime_dt(epoch_s):
    """epoch -> 'HH:MM' if today, 'dd/mm HH:MM' otherwise."""
    dt = datetime.datetime.fromtimestamp(epoch_s)
    if dt.date() == datetime.date.today():
        return dt.strftime("%H:%M")
    return dt.strftime("%d/%m %H:%M")


def task_label(payload):
    """Readable title of the task that originated the event (as in the app's list)."""
    session = str(first(payload, "session_id", "sessionId") or "")
    title = ""
    try:
        db = os.path.join(config.BASE, "v2", "tasks-index.sqlite")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT title FROM tasks WHERE task_id = ? AND deleted = 0 LIMIT 1",
                (session,)).fetchone()
        finally:
            conn.close()
        title = (row[0] or "").strip() if row else ""
    except Exception:  # noqa: BLE001 - the label is best-effort
        title = ""
    if not title:
        cwd = str(payload.get("cwd") or "")
        title = os.path.basename(cwd.rstrip("/")) if cwd else "ZCode"
    return title[:60]


def labeled(payload, body):
    return f"🧵 <b>{esc(task_label(payload))}</b>\n{body}"


def readable_duration(seconds):
    """F52 — «86400 s» is unreadable: 24 h / 45 min / 30 s. For the
    phone buttons' wait texts."""
    s = int(seconds)
    if s >= 3600 and s % 3600 == 0:
        return "%d h" % (s // 3600)
    if s >= 3600:
        return "%d h %02d min" % (s // 3600, (s % 3600) // 60)
    if s >= 60:
        return "%d min" % (s // 60)
    return "%d s" % s


def equal_parts(text, cap):
    """Splits a long text into N EQUAL parts by lines (N = ceil(len/cap),
    target = len/N) to read it in full via buttons — it is never
    truncated (F38c, Damian's idea; F40 shares it with the CoT). A line
    longer than the target is hard-cut; the tiny tail left by the line
    granularity is absorbed backwards while it fits under cap."""
    if len(text) <= cap:
        return [text]
    n = -(-len(text) // cap)
    target = -(-len(text) // n)
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > target:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:target])
            line = line[target:]
        if len(cur) + len(line) > target and cur:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    while len(parts) > n and len(parts[-1]) + len(parts[-2]) <= cap:
        parts[-2:] = [parts[-2] + parts[-1]]
    return parts


def lines_stamp(added, removed, lines=None):
    """F50b — the line-count stamp on EVERY file shown (Damian: «todo  # noqa: spanish — Damian's quoted request (verbatim)
    archivo que muestres, sea de diff o sea común, que tenga su número de  # noqa: spanish — Damian's quoted request (verbatim)
    líneas con redondel verde y rojo — o sea todos»). Diffs → 🟢 +N ·  # noqa: spanish — Damian's quoted request (verbatim)
    🔴 −M; plain file (or additionally) → N líneas. Accepts numstat  # noqa: spanish — Damian's quoted request (verbatim)
    strings (binaries come as '-')."""
    def _n(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    parts = []
    if _n(added):
        parts.append("🟢 +%d" % _n(added))
    if _n(removed):
        parts.append("🔴 −%d" % _n(removed))
    if lines is not None:
        parts.append("%d lines" % lines)
    return " · ".join(parts) or "—"


def html_clip(text, cap_html):
    """The longest RAW prefix whose ESCAPE fits in cap_html (audit
    20/10): tool box caps were measured raw and esc() inflates &<> up
    to 5x — the command/diff arrived cut in half by send()'s truncation.
    Binary search: esc() is monotonic in length."""
    if len(esc(text)) <= cap_html:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(esc(text[:mid])) <= cap_html:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def html_truncate(text, cap):
    """Cuts an HTML message to Telegram's cap WITHOUT breaking parsing
    (deep audit 20/10): send()'s plain truncation could land with an
    open <pre> (its close sits at the end: any overflow eats it) and
    Telegram rejects the WHOLE message with 400 «Unclosed tag» — plus
    the outbox retrying it forever. Also trims an entity (&amp;) or a
    tag split by the cut. Returns the text intact if it already fits
    in cap."""
    if len(text) <= cap:
        return text
    t = text[:cap]
    lt, gt = t.rfind("<"), t.rfind(">")
    if lt > gt:  # a tag was left half-open (<pr…): drop it
        t = t[:lt]
    amp, sem = t.rfind("&"), t.rfind(";")
    if amp > sem:  # a split entity (&am…): drop it
        t = t[:amp]
    open_tags = []
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>", t):
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            if name in open_tags:
                for i in range(len(open_tags) - 1, -1, -1):
                    if open_tags[i] == name:
                        del open_tags[i]
                        break
        elif not m.group(0).rstrip(">").endswith("/"):
            open_tags.append(name)
    return t + "".join("</%s>" % n for n in reversed(open_tags))
