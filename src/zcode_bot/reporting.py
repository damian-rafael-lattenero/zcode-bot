"""reporting — informes y formatters del bot (F4f).

Todo lo que el dueño lee: /quota /usage /ctx /task /compact /stop /status,
el trace de un turno y la entrega trozeada de respuestas largas. Lecturas
de sqlite/logs del CLI + formatters puros; transporte POR ATRIBUTO
(uhttp.send / uhttp.send_document): un solo seam de parcheo.
"""
import json
import os
import re
from dataclasses import dataclass
import time
import urllib.request

from zcode_bot import config
from zcode_bot import limites as lim
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import cli_logs as zc_logs
from zcode_bot import reports as zc_reports
from zcode_bot import avisos
from zcode_bot import proto as zc_proto
from zcode_bot.core import (active_bot_session, active_workspace_path,
                            session_title_from_db, _cur_sel, _model_snapshot,
                            _restore_model, _wait_cli_event, running_turns,
                            _timeout_por_mb, _timeout_compact, ultimo_uso,
                            _workers, _queue_peek, _queue_sids_with_items)
from zcode_bot.live_view import _live_skip_clear, _rss_now_mb
from zcode_bot.messages import (_steps_of_messages, _reasoning_of_messages,
                                _extract_assistant_text)
from zcode_bot.models import MODEL_SEL
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, preview, fmt_tok, fmt_num, datetime_dt
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log


# Ventana de contexto por modelo (tokens). GLM-5.3: 1M oficial (128K output).
CONTEXT_WINDOWS = {"GLM-5.3": 1000000, "GLM-5.3-Flash": 1000000}

def session_titles():
    """taskId -> título, desde el índice de sesiones de la app."""
    import glob as globmod
    titles = {}
    for path in globmod.glob(os.path.join(config.BASE, "v2", "sessions", "*", "ses_*.json")):
        try:
            with open(path) as f:
                meta = json.load(f).get("meta") or {}
            if meta.get("taskId"):
                titles[meta["taskId"]] = meta.get("title") or ""
        except (OSError, ValueError):
            continue
    return titles


def coding_usage(day_offset=0):
    """Usage real del coding plan desde los registros model-io del CLI
    (~/.zcode/cli/rollout/model-io-*.jsonl): usage por request con valores
    reales (el log de texto los redacta). La retención es rolling, así que
    la ventana devuelta dice desde qué hora alcanza el dato.
    Devuelve (totales, {sessionId: {req, total}}, (desde, hasta)) o (None, None, None).
    """
    import datetime
    import glob as globmod
    target = datetime.date.today() - datetime.timedelta(days=day_offset)
    tot = {"req": 0, "in": 0, "out": 0, "cache": 0, "total": 0}
    per_sess = {}
    window = [None, None]
    for path in globmod.glob(os.path.join(config.BASE, "cli", "rollout", "model-io-*.jsonl")):
        try:
            f = open(path, errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "coding-plan" not in str((d.get("model") or {}).get("providerId") or ""):
                    continue
                ts = d.get("completedAt") or ""
                try:
                    local = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue
                if local.date() != target:
                    continue
                u = (d.get("response") or {}).get("usage") or {}

                def _num(k):
                    try:
                        return int(u.get(k) or 0)
                    except (TypeError, ValueError):
                        return 0
                window[0] = window[0] or local.strftime("%H:%M")
                window[1] = local.strftime("%H:%M")
                tot["req"] += 1
                tot["in"] += _num("inputTokens")
                tot["out"] += _num("outputTokens")
                tot["cache"] += _num("cacheReadTokens")
                tot["total"] += _num("totalTokens")
                sid = str(d.get("sessionId") or "?")
                s = per_sess.setdefault(sid, {"req": 0, "total": 0})
                s["req"] += 1
                s["total"] += _num("totalTokens")
    if not tot["req"]:
        return None, None, None
    return tot, per_sess, tuple(window)


def read_quota_cache():
    """Lee el snapshot de quota del coding plan que el renderer de la app
    cachea en su Local Storage (leveldb). Es el mismo dato que muestra el
    panel Usage de la app: limits con currentValue (usados), usage (límite),
    percentage y nextResetTime por ventana. El snapshot se reescribe en el
    .log vivo del leveldb como texto plano; buscamos la última aparición.
    Devuelve dict {level, windows:[{used,limit,remaining,pct,reset_ms}], mcp} o None.
    """
    import datetime
    ldir = os.path.expanduser("~/Library/Application Support/ZCode/session/Local Storage/leveldb")
    if not os.path.isdir(ldir):
        return None
    files = []
    for name in os.listdir(ldir):
        if name.endswith(".log") or name.endswith(".ldb"):
            p = os.path.join(ldir, name)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
    marker = b'"quota":{"level"'
    best = None  # (nextResetTime máximo visto → entrada más fresca)
    for _, path in sorted(files, reverse=True):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        pos = 0
        while True:
            i = data.find(marker, pos)
            if i < 0:
                break
            pos = i + 1
            # extraer el objeto JSON balanceado que arranca en "quota":
            start = i + len(b'"quota":')
            depth = 0
            end = None
            in_str = False
            esc = False
            for j in range(start, min(start + 8000, len(data))):
                c = data[j:j + 1]
                if esc:
                    esc = False
                    continue
                if c == b"\\":
                    esc = True
                    continue
                if c == b'"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c == b"{":
                    depth += 1
                elif c == b"}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            if not end:
                continue
            try:
                q = json.loads(data[start:end])
            except ValueError:
                continue
            if not isinstance(q, dict) or "limits" not in q:
                continue
            freshest = max((l.get("nextResetTime") or 0) for l in q["limits"])
            if best is None or freshest > best[0]:
                best = (freshest, q)
    if not best:
        return None
    q = best[1]
    windows = []
    for lim in q.get("limits") or []:
        if lim.get("type") != "CREDIT_LIMIT":
            continue
        windows.append({
            "hours": lim.get("number"),  # 5 → ventana 5h, 1 → semanal
            "used": lim.get("currentValue"),
            "limit": lim.get("usage"),
            "remaining": lim.get("remaining"),
            "pct": lim.get("percentage"),
            "reset_ms": lim.get("nextResetTime"),
        })
    windows.sort(key=lambda w: (w["hours"] or 0), reverse=True)  # 5h primero, semana después
    mcp = None
    agg = q.get("mcpAggregate") or q.get("mcp_aggregate") or {}
    if isinstance(agg, dict) and agg.get("limit"):
        mcp = {"used": agg.get("used") or 0, "limit": agg.get("limit")}
    return {"level": q.get("level"), "windows": windows, "mcp": mcp}


def _quota_api_key():
    """API key del coding plan desde bot-provider.json (nunca loguear)."""
    try:
        with open(os.path.join(config.BASE, "hooks", "bot-provider.json")) as f:
            cfg = json.load(f)
        for rule in ((((cfg.get("config") or {}).get("providerConfigRules")
                       or {}).get("providerRules")) or []):
            acc = (rule.get("config") or {}).get("access") or {}
            if acc.get("type") == "zhipu-coding-plan-api-key" and acc.get("apiKey"):
                return acc["apiKey"]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def quota_live():
    """Quota EN VIVO del endpoint del panel (Bearer con la API key del bot).
    Devuelve {"level","windows":[{hours,used,limit,pct,reset_ms}]} o None.
    No depende de que la app haya escrito su cache (el leveldb se compacta
    y el snapshot desaparece — pasó el 11/10)."""
    key = _quota_api_key()
    if not key:
        return None
    for intento in range(3):
        try:
            req = urllib.request.Request(
                "https://api.z.ai/api/monitor/usage/quota/limit",
                headers={"Authorization": "Bearer " + key,
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = (json.loads(r.read().decode()).get("data")) or {}
            out = {"level": data.get("level"), "windows": []}
            for w in data.get("limits") or []:
                used = w.get("currentValue")
                limit = w.get("usage")
                if not isinstance(used, (int, float)) or not limit:
                    continue
                out["windows"].append({
                    "hours": w.get("number"),
                    "used": used, "limit": limit,
                    "pct": w.get("percentage"),
                    "reset_ms": w.get("nextResetTime")})
            return out if out["windows"] else None
        except Exception as exc:  # noqa: BLE001 - red flaky: reintentar
            log("errors", "quota_live intento %d: %s" % (intento + 1, str(exc)[:120]))
            time.sleep(1 + intento)
    return None


def quota_report():
    """Coding plan: SOLO credits 5h + weekly con resets (como el panel Usage).
    Fuente primaria: endpoint en vivo; fallback: cache del panel de la app.
    El detalle de tokens por sesión vive en /usage."""
    lines = ["📊 <b>Coding plan</b>"]
    snap = quota_live() or read_quota_cache()
    if snap:
        nombres = {5: "⏱ 5 horas", 1: "📅 Semanal"}
        for w in snap["windows"]:
            nombre = nombres.get(w["hours"], "🔒 Límite")
            pct = f" ({w['pct']}%)" if w.get("pct") is not None else ""
            alerta = " ⚠️" if (w.get("pct") or 0) >= 75 else ""
            reset = ""
            if w.get("reset_ms"):
                reset = " · resetea " + datetime_dt(w["reset_ms"] / 1000)
            lines.append(f"{nombre}: {fmt_num(w['used'])}/{fmt_num(w['limit'])}"
                         f" credits{pct}{reset}{alerta}")
    else:
        lines.append("(sin datos — no pude ni al endpoint ni al cache)")
    lines.append("Detalle por sesión: /usage")
    return "\n".join(lines)


def usage_report():
    """Detalle del consumo de hoy por sesión (coding plan)."""
    tot, per_sess, window = coding_usage(0)
    if not tot:
        return "📈 Sin consumo registrado hoy todavía."
    lines = [f"📈 <b>Uso hoy por sesión</b> ({window[0]}–{window[1]})"]
    titles = session_titles()
    filas = sorted(per_sess.items(), key=lambda kv: -kv[1]["total"])
    for sid, s in filas[:8]:
        nombre = esc(titles.get(sid) or ("…" + sid[-10:]))
        lines.append(f"• {nombre}: {s['req']} req · {fmt_tok(s['total'])}")
    if len(filas) > 8:
        resto = sum(s["total"] for _, s in filas[8:])
        lines.append(f"• (+{len(filas) - 8} sesiones: {fmt_tok(resto)})")
    lines.append(f"<b>Total: {tot['req']} req · {fmt_tok(tot['total'])} tokens</b>")
    return "\n".join(lines)


@dataclass
class Task:
    """Fila del índice de tasks (frontera sqlite → reportes/listas).
    Compat mapping (get/keys/__getitem__): los consumidores que aún
    indexan t["sid"] siguen andando mientras migran a atributos."""
    sid: str = ""
    title: str = ""
    ws: str = ""
    model: str = ""
    ctx: int = 0
    when: int = 0  # ms epoch de la última actividad
    n: int = 0     # posición visible (la renumera _tasks_scope por scope)

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        t = cls()
        for k in ("sid", "title", "ws", "model"):
            setattr(t, k, str(d.get(k) or ""))
        t.ctx = int(d.get("ctx") or 0)
        t.when = int(d.get("when") or 0)
        t.n = int(d.get("n") or 0)
        return t

    def keys(self):
        return ("sid", "title", "ws", "model", "ctx", "when", "n")

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def get(self, k, default=None):
        return getattr(self, k, default)


def _ctx_tasks(limit=10):
    """Tasks con consumo real, sin archivadas, en el orden de la barra
    lateral de la app (Repository: zcode_bot.tasks_repo). Prefijo _: el
    nombre público ctx_tasks vive SOLO en tasks_repo (gate anti-dup)."""
    return [Task.from_dict(t)
            for t in zc_repo.ctx_tasks(config.BASE, limit=limit)]


def _compactiones(horas=48):
    """{sessionId: epoch} del último compact.completed por sesión (scanner
    Strategy: zcode_bot.cli_logs)."""
    lim = time.time() - horas * 3600
    out = {}
    for ts, sid, _ev, _d in zc_logs.iter_events(
            config.BASE, needles=("compact.completed",), since_epoch=lim,
            last_files=2, tail_bytes=0):
        out[sid] = ts
    return out


def context_report():
    """Contexto por task, numerado como la lista de /compact. Fuente: la
    última usage del rollout de cada sesión (lo que el modelo vio en su
    request más reciente — el número que verá el próximo turno). Si hubo
    un compact MÁS NUEVO que ese request, la sesión quedó compactada sin
    turno posterior: el único número en disco es el de ANTES, y se avisa
    en vez de mostrarlo como actual (16/10: el probe de session/resume
    devolvía contextUsed=0 siempre — el runtime no persiste la proyección
    — y /ctx caía al último turno pre-compact pagando un resume de 35s y
    3GB por consulta; retirado)."""
    import datetime
    tasks = _ctx_tasks(8)
    if not tasks:
        return "🧠 Sin tasks con uso registrado."
    act = active_workspace_path()
    comp = _compactiones()
    lines = ["🧠 <b>Contexto por task</b> (estado real en disco)"]
    for t in tasks:
        window = CONTEXT_WINDOWS.get(t["model"], 1000000)
        cts = comp.get(t["sid"])
        uso = ultimo_uso(t["sid"])
        if uso and cts and uso[1] <= cts:
            uso = None  # lo último en disco es anterior al compact
        tag = "" if t["ws"] == act else " · 📁" + esc(os.path.basename(t["ws"] or ""))
        titulo = esc(t["title"] or t["sid"][-8:])
        if uso is None and cts and t["when"] / 1000 <= cts:
            hc = datetime.datetime.fromtimestamp(cts).strftime("%H:%M")
            antes = f" (antes {fmt_tok(t['ctx'])})" if t["ctx"] else ""
            lines.append(f"{t['n']}. 🧹 compactada {hc} — el tamaño real se"
                         f" ve en el próximo turno{antes}{tag} — {titulo}")
            continue
        ctx = int(uso[0] * 1000) if uso else t["ctx"]
        pct = ctx * 100.0 / window
        emoji = "🔴" if pct >= 80 else ("🟡" if pct >= 50 else "🟢")
        lines.append(f"{t['n']}. {emoji} {fmt_tok(ctx)} ({pct:.0f}%){tag}"
                     f" — {titulo}")
    lines.append("\n/compact &lt;n&gt; compacta la task n (la app solo refleja"
                 " el compact al reabrir la task).")
    return "\n".join(lines)


def compact_list_report(scope="projects"):
    """/compact → lista tocable (Template Method: zcode_bot.reports)."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    activos = running_turns()
    act = active_bot_session()

    def marca_de(t):
        return ("🔄" if t["sid"] in activos
                else ("🎯" if act and t["sid"] == act.get("sid") else "·"))

    def line_of(t, i):
        return "%s %d. %s — %s" % (marca_de(t), t["n"],
                                   esc(t["title"] or t["sid"][-8:]),
                                   fmt_tok(t["ctx"]))

    def button_of(t, i):
        return {"text": "%s %d · %s (%s)" % (marca_de(t), t["n"],
                                             (t["title"] or t["sid"][-8:])[:28],
                                             fmt_tok(t["ctx"])),
                "callback_data": "c:%s" % t["sid"]}

    return zc_reports.build_list_report(
        tasks,
        "🧹 <b>Compactar — tocá una</b> (solo idle)"
        + (" · /all_compact para todas" if scope == "all" else ""),
        "🧠 Sin tasks%s para compactar — /all_compact muestra todas."
        % ("" if scope == "all" else " de proyecto"),
        line_of, button_of,
        "🔄 no se puede (está corriendo) · 🎯 es la activa.")


def _compact_args(rest, scope):
    """/compact [<n|@sid>] [instrucciones] → zcode_compact."""
    n = sid = None
    resto = rest
    head = rest.split()[0]
    if head.startswith("@ses"):
        sid = head[1:]
        resto = rest.split(" ", 1)[1].strip() if " " in rest else ""
    elif head.isdigit():
        n = int(head)
        resto = rest.split(" ", 1)[1].strip() if " " in rest else ""
    return zcode_compact(resto, n=n, sid=sid, scope=scope)


def zcode_compact(rest="", n=None, sid=None, scope="projects"):
    """Compacta por el ZCode Protocol (headless: sin UI, sin permisos,
    funciona con la pantalla bloqueada). Target: sid (botones) > n (posición
    de la lista del scope) > conversación activa > task más reciente del
    proyecto activo. Solo sesiones idle. Snapshot/restore del model
    selection para no contaminar sesiones de la app."""
    import sys as _sys
    import time as _t
    if sid is not None:
        target = {t["sid"]: t for t in _ctx_tasks(30)}.get(sid)
        if target is None:
            return "⚠️ No encontré esa conversación — mandá /compact."
    elif n is not None:
        tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
        if n < 1 or n > len(tasks):
            return ("⚠️ Número fuera de la lista (1 a %d) — mandá /compact."
                    % len(tasks))
        target = tasks[n - 1]
    else:
        act = active_bot_session()
        target = None
        if act and act.get("sid"):
            target = {t["sid"]: t for t in _ctx_tasks(30)}.get(act["sid"])
        if target is None:
            aw = active_workspace_path()
            cands = [t for t in _ctx_tasks(20) if t["ws"] == aw]
            if not cands:
                return ("🤔 No hay tasks con uso en el proyecto activo — "
                        "mandá /compact para elegir.")
            target = cands[0]
    sid, ws, titulo = target["sid"], target["ws"], target["title"]
    try:
        c = zc_proto.make(cwd=ws)
    except OSError as e:
        return "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120])
    snap_sel = None
    try:
        r = c.request("session/list", {}, timeout=30)
        if e := zc_proto.proto_err(r, "session/list"):
            return e
        st = {x["sessionId"]: x.get("status")
              for x in (r.get("result") or {}).get("sessions", [])}
        if st.get(sid) == "running":
            return ("⚠️ «%s» está corriendo — frená primero (/stop o el botón "
                    "Stop) y reintentá." % esc(titulo))
        # F36: ack «llegó tu elección» vía el componente (avisos) — la app
        # ya aceptó el pedido (sesión idle confirmada). Va antes del
        # resume (35s en sesiones grandes) y del compact (minutos).
        avisos.eleccion_recibida(
            load_config(), "compactando «%s»" % titulo,
            "te aviso al terminar (estado con /ctx)")
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=_timeout_por_mb(sid))
        if e := zc_proto.proto_err(r, "resume"):
            return e
        snap_sel = _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": MODEL_SEL},
                      timeout=_timeout_por_mb(sid))
        if e := zc_proto.proto_err(r, "setModel"):
            return e
        params = {"sessionId": sid}
        if rest:
            params["instructions"] = rest
        started = _t.time()
        # techo por tokens (regla de Damian calibrada: 1.2 s/1k tokens);
        # el evento de fin se espera con el mismo presupuesto +30
        t_comp = _timeout_compact(sid)
        r = c.request("session/compact", params, timeout=t_comp)
        if e := zc_proto.proto_err(r, "compact"):
            return e
        fin = _wait_cli_event(sid, started,
                              ("compact.completed", "compact.failed"),
                              t_comp + 30)
        if fin is None:
            return ("🤔 Compactación enviada pero no vi el evento de fin en "
                    "%d s. Verificá con /ctx." % (t_comp + 30))
        if fin.get("event") == "compact.failed":
            err = str((fin.get("error") or {}).get("message", ""))[:120]
            return "❌ La compactación de «%s» falló: %s" % (esc(titulo), esc(err))
        # el compact achica el replay de session/events: rehabilitar el
        # CoT en vivo para los próximos turnos de esta sesión
        _live_skip_clear(sid)
        dur = (fin.get("durationMs") or 0) / 1000.0
        if dur < 2:
            return ("🧹 «%s» compactada (al instante: no había nada nuevo "
                    "que resumir). Verificá con /ctx." % esc(titulo))
        # números REALES: la compactación deja un part "compaction" en el
        # historial con preCompactTokenCount/postCompactTokenCount exactos
        # (mejor que estimar desde model_usage)
        pre = post = None
        try:
            r = c.request("session/messages", {"sessionId": sid, "limit": 15},
                          timeout=30)
            for m in (r.get("result") or {}).get("messages") or []:
                for p in m.get("parts", []):
                    if p.get("type") == "compaction" and p.get("operationId"):
                        pre = p.get("preCompactTokenCount")
                        # truePost = contexto REAL restante; postCompact puede
                        # ser MAYOR que pre (conteo sin recorte) y confundir
                        post = (p.get("truePostCompactTokenCount")
                                or p.get("postCompactTokenCount"))
        except Exception:  # noqa: BLE001
            pass
        nums = ""
        if pre and post:
            nums = " Contexto: %s → %s." % (fmt_tok(pre), fmt_tok(post))
        return ("🧹 Compactación de «%s» lista en %.0fs.%s\n"
                "⚠️ Si esta task está abierta en la app, reabrila — su ventana "
                "sigue con el estado anterior hasta reiniciar la task."
                % (esc(titulo), dur, nums))
    finally:
        c.close()
        if snap_sel:
            _restore_model(sid, snap_sel)


def _tasks_scope(scope, limit):
    """/task → solo tasks con Project (excluye las sueltas del workspace
    default); /all_tasks → todas bajo el grupo Tasks."""
    tasks = _ctx_tasks(limit)
    if scope != "all":
        tasks = [t for t in tasks if t["ws"] != config.DEFAULT_WS]
    # renumerar según la lista QUE SE MUESTRA: el n tipeado (/task n) tiene
    # que calzar con el número visible, no con la posición en la lista completa
    for i, t in enumerate(tasks, 1):
        t.n = i
    return tasks


def task_list_report(scope="projects"):
    """/task → lista tocable (Template Method: zcode_bot.reports)."""
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    activos = running_turns()
    act = active_bot_session()

    def marca_de(t):
        return ("🔄" if t["sid"] in activos
                else ("🎯" if act and t["sid"] == act.get("sid") else "·"))

    def line_of(t, i):
        tag = "" if t["ws"] == active_workspace_path() \
            else " · 📁" + esc(os.path.basename(t["ws"] or ""))
        return "%s %d. %s%s" % (marca_de(t), t["n"],
                                esc(t["title"] or t["sid"][-8:]), tag)

    def button_of(t, i):
        return {"text": "%s %d · %s" % (marca_de(t), t["n"],
                                        (t["title"] or t["sid"][-8:])[:34]),
                "callback_data": "t:%s" % t["sid"]}

    header = ("💬 <b>Todas las conversaciones</b> (incluye sueltas) — /task solo proyectos"
              if scope == "all"
              else "💬 <b>Conversaciones por proyecto</b> — tocá una (o /task n)")
    return zc_reports.build_list_report(
        tasks, header,
        "🧠 Sin tasks%s — /all_tasks muestra todas."
        % ("" if scope == "all" else " de proyecto"),
        line_of, button_of,
        "Texto libre que mandes va a la conversación activa (🎯).")


def stop_list_report():
    """/stop → corridas del celular + colas dormidas (Template Method)."""
    items = _workers().items()
    colas = _queue_sids_with_items()

    def mins_de(par):
        return max(0, int((time.time() - par[1].get("t0", time.time())) / 60))

    def line_of(par, i):
        sid, w = par
        n = len(_queue_peek(sid))
        cola = ("\n    ⏳ %d encolado%s (se limpia al frenar)" % (n, "s" if n != 1 else "")) if n else ""
        return "%d. «%s» — %d min\n    %s%s" % (
            i, esc(w.get("titulo") or sid[-8:]), mins_de(par),
            esc(preview(w.get("prompt") or "", 70)), cola)

    def button_of(par, i):
        sid, w = par
        return {"text": "⏹ %d · %s (%dm)" % (i,
                                              (w.get("titulo") or sid[-8:])[:30],
                                              mins_de(par)),
                "callback_data": "s:%s" % sid}

    # sesiones con cola pero SIN worker: la corrida es de la Mac (no se
    # frena desde acá) — ofrecer igual la limpieza de su cola
    extras = [(sid, None) for sid in colas
              if _workers().get(sid) is None]

    def line_cola(par, i):
        sid, _ = par
        act = active_bot_session()
        marca = " 🎯" if act and sid == act.get("sid") else ""
        n = len(_queue_peek(sid))
        return "%d. ⏳ Cola de «%s»%s — %d mensaje%s (corrida de la Mac)" % (
            i, esc(sid[-8:]), marca, n, "s" if n != 1 else "")

    def button_cola(par, i):
        return {"text": "🧹 %d · limpiar cola" % i,
                "callback_data": "s:%s" % par[0]}

    cuerpo = zc_reports.build_list_report(
        items, "🛑 <b>Corridas del celular — tocá para frenar</b>",
        "😌 No hay nada corriendo iniciado desde el celular.\n"
        "Lo que corre en la Mac se frena con el botón Stop de la app.",
        line_of, button_of) if items else None
    cola_report = zc_reports.build_list_report(
        extras, "📬 <b>Mensajes en cola (Mac ocupada) — tocá para limpiar</b>",
        None, line_cola, button_cola) if extras else None
    if cuerpo and cola_report:
        return cuerpo + "\n\n" + cola_report
    return cuerpo or cola_report or zc_reports.build_list_report(
        [], "🛑 <b>Corridas del celular — tocá para frenar</b>",
        "😌 No hay nada corriendo iniciado desde el celular.\n"
        "Lo que corre en la Mac se frena con el botón Stop de la app.",
        line_of, button_of)


def build_trace(titulo, sid, dur_min, msgs, live_cot=""):
    """Documento .txt del turno: pasos numerados + razonamiento completo +
    respuesta. Autoritativo: el historial (msgs); live_cot es fallback si
    el historial no trajera parts de reasoning."""
    import datetime
    steps = _steps_of_messages(msgs)
    cot = _reasoning_of_messages(msgs) or live_cot
    answer = _extract_assistant_text(msgs)
    L = ["╔═ TURNO: %s" % (titulo or "?"),
         "║ sesión: %s" % (sid or "?"),
         "║ %s · %.1f min" % (datetime.datetime.now().strftime(
             "%Y-%m-%d %H:%M"), dur_min),
         "╚" + "═" * 26, ""]
    L.append("════════ PASOS (%d) ════════" % len(steps))
    for i, (nombre, desc) in enumerate(steps, 1):
        L.append("%3d. %s%s" % (i, nombre, (" · " + desc) if desc else ""))
    L.append("")
    L.append("════════ RAZONAMIENTO COMPLETO (%d chars) ════════" % len(cot))
    L.append(cot or "(sin razonamiento registrado)")
    L.append("")
    L.append("════════ RESPUESTA ════════")
    L.append(answer or "(sin texto de respuesta)")
    return "\n".join(L)


def _turn_trace(cfg, titulo, sid, dur_min, msgs, live_cot=""):
    """Al terminar un turno: persiste el trace y LO MANDA SOLO si
    trace_documents=true (default OFF desde F19 — pedido de Damian: el
    CoT ya queda scrolleable en las cajas del chat; el archivo era ruido
    redundante. /cot lo genera on-demand cuando se pide). El archivo
    state/last-turn-trace.txt queda siempre."""
    try:
        trace = build_trace(titulo, sid, dur_min, msgs, live_cot=live_cot)
    except Exception as exc:  # noqa: BLE001 - nunca romper la entrega
        log("errors", "build_trace: %s" % exc)
        return
    try:
        with open(os.path.join(config.STATE_DIR, config.ARCH_ULTIMO_TRACE), "w") as f:
            f.write(trace)
    except OSError:
        pass
    if not cfg.get("trace_documents", False):
        return
    import datetime
    fname = "turn-%s-%s.txt" % (
        re.sub(r"[^A-Za-z0-9._-]+", "-", (titulo or "ses")[:24]).strip("-"),
        datetime.datetime.now().strftime("%H%M"))
    np = len(_steps_of_messages(msgs))
    ncot = len(_reasoning_of_messages(msgs) or live_cot)
    uhttp.send_document(cfg, fname, trace,
                  "🧠 Trace «%s»: %d pasos · CoT %s chars · respuesta aparte"
                  % (titulo or "?", np, fmt_tok(ncot)))


def _send_long_reply(cfg, titulo, dur_min, texto, max_partes=3):
    """Respuesta completa troceada en mensajes de ~3900 chars (máx 3 +
    aviso con /last para el resto). Guarda el texto completo para /last."""
    total = len(texto)
    partes = [texto[i:i + lim.CH_RESPUESTA]
              for i in range(0, total, lim.CH_RESPUESTA)]
    try:
        with open(os.path.join(config.STATE_DIR, config.ARCH_ULTIMA_RESPUESTA), "w") as f:
            f.write(texto)
    except OSError:
        pass
    if len(partes) > max_partes:
        resto = sum(len(p) for p in partes[max_partes:])
        partes = partes[:max_partes]
        cola = "\n\n(+%d caracteres más — mandá /last)" % resto
    else:
        cola = ""
    for i, p in enumerate(partes):
        # 🤖 bot: origen explícito (los turnos de la Mac dicen 🖥️ Mac — F32)
        pre = ("✅ «%s» listo en %.0f min · 🤖 bot" % (esc(titulo), dur_min)
               ) if i == 0 else "···"
        suf = " (%d/%d)" % (i + 1, len(partes)) if len(partes) > 1 else ""
        uhttp.send(cfg, pre + suf + "\n" + esc(p) + (cola if i == len(partes) - 1 else ""),
                   loud=False)


def status_report():
    act = active_bot_session()
    lines = ["📊 <b>Estado</b>"]
    if act:
        tag = os.path.basename(act.get("ws") or "")
        lines.append("🎯 Activa: «%s»%s" % (esc(act.get("title", "")),
                                           " · 📁" + esc(tag) if tag else ""))
        # F22: configuración de la activa a la vista
        sel = _cur_sel(act)
        lines.append("🤖 %s · 🧠 %s · 🎛 %s"
                     % (esc(sel.get("modelId", "?")),
                        esc((sel.get("options") or {}).get("reasoningLevel",
                                                           "high")),
                        esc(_modo_label(act.get("mode") or "yolo"))))
    else:
        lines.append("🎯 Activa: ninguna (/task n para elegir)")
    workers = _workers().items()
    if workers:
        lines.append("🤖 <b>Workers del bot</b>:")
        for sid, w in workers:
            mins = max(0, int((time.time() - (w.get("t0") or time.time())) / 60))
            lines.append("  • «%s» pid %d · %d min · %s"
                         % (esc(w.get("titulo") or sid[-8:]), w.get("pid") or 0,
                            mins, esc(preview(w.get("prompt") or "", 50))))
    for sid in _queue_sids_with_items():
        if _workers().get(sid) is None:  # colas dormidas (las vivas las ve arriba)
            n = len(_queue_peek(sid))
            act_sid = (act or {}).get("sid")
            marca = " 🎯" if act and sid == act_sid else ""
            lines.append("📬 Cola dormida «%s»%s — %d mensaje%s (/queue)"
                         % (esc(sid[-8:]), marca, n, "s" if n != 1 else ""))
    activos = running_turns()
    if activos:
        for sid, ts in activos.items():
            if _workers().get(sid) is not None:
                continue  # ya listado como worker con detalle
            titulo = session_title_from_db(sid) or sid[-8:]
            import datetime
            lines.append("🔄 «%s» desde %s (corrida de la Mac)"
                         % (esc(titulo),
                            datetime.datetime.fromtimestamp(ts).strftime("%H:%M")))
    if not workers and not activos and not _queue_sids_with_items():
        lines.append("😌 Nada corriendo.")
    lines.append("🫀 poller %d · arriba %s · RSS %dMB"
                 % (os.getpid(), _uptime_txt(), _rss_now_mb()))
    return "\n".join(lines)


def _uptime_txt():
    s = int(time.time() - _POLLER_T0)
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def _modo_label(mode):
    return dict(MODOS).get(mode or "yolo", mode or "yolo")


def _resolve_ws(tag):
    """Resuelve un tag de proyecto ('bot', 'cv', haskell...) a path."""
    act = active_workspace_path()
    if not tag:
        return act
    tagl = tag.strip().strip("/").lower()
    cands = set([act]) | {t["ws"] for t in _ctx_tasks(20) if t.get("ws")}
    for w in cands:
        if w and os.path.basename(w).lower() == tagl:
            return w
    for w in cands:
        if w and tagl in os.path.basename(w).lower():
            return w
    return None


_POLLER_T0 = time.time()  # arranque del proceso (para /status)


MODOS = (("plan", "🗺 Plan mode"), ("yolo", "🔓 Full access"))

