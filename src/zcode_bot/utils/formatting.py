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


def duracion_legible(segundos):
    """F52 — «86400 s» no se lee: 24 h / 45 min / 30 s. Para los textos
    de espera de los botones del celu."""
    s = int(segundos)
    if s >= 3600 and s % 3600 == 0:
        return "%d h" % (s // 3600)
    if s >= 3600:
        return "%d h %02d min" % (s // 3600, (s % 3600) // 60)
    if s >= 60:
        return "%d min" % (s // 60)
    return "%d s" % s


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


def sello_lineas(agregadas, quitadas, lineas=None):
    """F50b — el sello de líneas de TODO archivo mostrado (Damian: «todo
    archivo que muestres, sea de diff o sea común, que tenga su número de
    líneas con redondel verde y rojo — o sea todos»). Diffs → 🟢 +N ·
    🔴 −M; archivo común (o además) → N líneas. Acepta los strings de
    numstat (binarios traen '-')."""
    def _n(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    partes = []
    if _n(agregadas):
        partes.append("🟢 +%d" % _n(agregadas))
    if _n(quitadas):
        partes.append("🔴 −%d" % _n(quitadas))
    if lineas is not None:
        partes.append("%d líneas" % lineas)
    return " · ".join(partes) or "—"


def recorte_html(texto, cap_html):
    """El mayor prefijo CRUDO cuyo ESCAPE entra en cap_html (auditoría
    20/10): los caps de las cajas de tools se medían en crudo y esc()
    infla &<> hasta 5x — el comando/diff llegaba cortado a mitad por el
    truncado de send(). Búsqueda binaria: esc() es monótono en el largo."""
    if len(esc(texto)) <= cap_html:
        return texto
    lo, hi = 0, len(texto)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(esc(texto[:mid])) <= cap_html:
            lo = mid
        else:
            hi = mid - 1
    return texto[:lo]


def truncar_html(texto, cap):
    """Corta un mensaje HTML al tope de Telegram SIN romper el parseo
    (auditoría profunda 20/10): el truncado plano de send() podía caer
    con un <pre> abierto (su cierre queda al final: cualquier overflow
    se lo come) y Telegram rechaza el mensaje ENTERO con 400 «Unclosed
    tag» — más el outbox reintentándolo para siempre. También recorta
    una entidad (&amp;) o un tag partidos por el corte. Devuelve el
    texto intacto si ya entra en cap."""
    if len(texto) <= cap:
        return texto
    t = texto[:cap]
    lt, gt = t.rfind("<"), t.rfind(">")
    if lt > gt:  # un tag quedó abierto por la mitad (<pr…): fuera
        t = t[:lt]
    amp, sem = t.rfind("&"), t.rfind(";")
    if amp > sem:  # una entidad partida (&am…): fuera
        t = t[:amp]
    abiertas = []
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>", t):
        cier_re, nombre = m.group(1), m.group(2).lower()
        if cier_re:
            if nombre in abiertas:
                for i in range(len(abiertas) - 1, -1, -1):
                    if abiertas[i] == nombre:
                        del abiertas[i]
                        break
        elif not m.group(0).rstrip(">").endswith("/"):
            abiertas.append(nombre)
    return t + "".join("</%s>" % n for n in reversed(abiertas))
