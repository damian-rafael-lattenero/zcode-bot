"""Utilidades de formateo de texto para zcode_bot."""
import json
import re
import datetime
import os
import sqlite3

from zcode_bot import config
from .validation import first


def esc(s):
    """Escapa HTML para Telegram (parse_mode=HTML). Fuente única: el
    gateway importaba esta copia rota (entidades perdidas en un
    copy-paste) mientras usaba su def local — consolidado 15/10."""
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
    """Para credits del panel: 880 -> '880' · 28830 -> '28.8K' (K recién desde 10K)"""
    if n is None:
        return "?"
    n = float(n)
    if n >= 1e4:
        v = n / 1e3
        return ("%.1fK" % v).replace(".0K", "K")
    return str(int(n))


def datetime_dt(epoch_s):
    """epoch -> 'HH:MM' si es hoy, 'dd/mm HH:MM' si no."""
    dt = datetime.datetime.fromtimestamp(epoch_s)
    if dt.date() == datetime.date.today():
        return dt.strftime("%H:%M")
    return dt.strftime("%d/%m %H:%M")


def task_label(payload):
    """Título legible de la tarea que originó el evento (como en la lista de la app)."""
    session = str(first(payload, "session_id", "sessionId") or "")
    title = ""
    try:
        db = os.path.join(config.BASE, "v2", "tasks-index.sqlite")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT title FROM tasks WHERE task_id = ? AND deleted = 0 LIMIT 1",
                (session,)).fetchone()
        finally:
            con.close()
        title = (row[0] or "").strip() if row else ""
    except Exception:  # noqa: BLE001 - etiqueta es best-effort
        title = ""
    if not title:
        cwd = str(payload.get("cwd") or "")
        title = os.path.basename(cwd.rstrip("/")) if cwd else "ZCode"
    return title[:60]


def labeled(payload, body):
    return f"🧵 <b>{esc(task_label(payload))}</b>\n{body}"


def partes_iguales(texto, cap):
    """Parte un texto largo en N partes IGUALES por líneas (N = techo(len/cap),
    objetivo = len/N) para verlo completo por partes con botones — nunca se
    recorta (F38c, idea de Damian; F40 lo comparte con el CoT). Una línea
    más larga que el objetivo se corta en duro; la cola enana que deja la
    granularidad por líneas se absorbe hacia atrás mientras quepa bajo cap."""
    if len(texto) <= cap:
        return [texto]
    n = -(-len(texto) // cap)
    objetivo = -(-len(texto) // n)
    partes, actual = [], ""
    for linea in texto.splitlines(keepends=True):
        while len(linea) > objetivo:
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
    while len(partes) > n and len(partes[-1]) + len(partes[-2]) <= cap:
        partes[-2:] = [partes[-2] + partes[-1]]
    return partes
