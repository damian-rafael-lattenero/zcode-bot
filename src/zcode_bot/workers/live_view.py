"""workers.live_view — live view del turno headless (CoT, tool calls, etc.)."""
import json
import os
import re
import time
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview, fmt_tok
from zcode_bot.utils.logging import pwlog

from zcode_bot import proto as zc_proto
from zcode_bot import fswatch
from zcode_bot import cli_logs as zc_logs

from zcode_bot.state import WORKERS, QUESTIONS
from zcode_bot.state.manager import (_turn_end_poll, _live_skip_check, _live_skip_mark,
    _live_skip_clear, _rss_mb, _rss_now_mb, _active_turn_register,
    _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text,
    _orphans_check, _q_gemela, _user_input_handler, _headless_text,
    _task_current_ok)


COT_CHUNK = 3000
COT_TAIL = 3200
EVENTS_TRIP = 1200
LIVE_FRAME_TRIP = 25 * 1024 * 1024
LIVE_SKIP_TTL = 6 * 3600
LIVE_BIG_FRAME = 2 * 1024 * 1024


def _poll_cadence(max_frame):
    return 5.0 if max_frame > 2 * 1024 * 1024 else 2.0


def _live_skip_path():
    return os.path.join(STATE_DIR, "bot-live-skip.json")


def _live_skip_all():
    try:
        with open(_live_skip_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _live_skip_mark(sid, now=None):
    now = now or time.time()
    d = {}
    try:
        with open(_live_skip_path()) as f:
            d = json.load(f)
    except (OSError, ValueError):
        pass
    d[sid] = now
    tmp = _live_skip_path() + ".tmp"
    try:
        os.makedirs(os.path.dirname(_live_skip_path()), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _live_skip_path())
    except OSError:
        pass


def _live_skip_check(sid, now=None):
    ts = _live_skip_all().get(sid)
    return bool(ts) and (now or time.time()) - ts < 6 * 3600


def _live_skip_clear(sid):
    d = _live_skip_all()
    if sid in d:
        del d[sid]
        tmp = _live_skip_path() + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, _live_skip_path())
        except OSError:
            pass


HEADLESS_GUARD = (
    "\n\n—\n⚠️ (aviso del bot que ejecuta este mensaje: corrés HEADLESS dentro "
    "de un runtime zcode propio. NO mates ni reinicies procesos de zcode — "
    "kill/pkill/killall sobre zcode o app-server matan a TU PROPIO huésped y "
    "este turno muere con «Turn was cancelled» (incidentes reales 2026-10-13 "
    "14:04, 15:12 y 16:13). launchctl ya está BLOQUEADO en tu sandbox: ni lo "
    "intentes. Si el bot o el poller parecen trabados, NO los reinicies: "
    "avisale al usuario por el chat y que use /nuclear desde Telegram. Para "
    "deployar cambios: commit + push y avisarle — deploy.sh desde acá difiere "
    "la recarga solo.)"
)


def _headless_text(text, cfg):
    if not cfg.get("headless_guard", True):
        return text
    if HEADLESS_GUARD[:20] in (text or "")[-600:]:
        return text
    return (text or "") + HEADLESS_GUARD


def _cot_chunks_to_send(cot_full, flushed, tail_keep=3200, ch=3000):
    out = []
    while len(cot_full) - flushed > tail_keep:
        trozo = cot_full[flushed:flushed + ch]
        out.append(trozo)
        flushed += len(trozo)
    return out, flushed


def _steps_of_messages(msgs):
    return [(n, _tool_desc_corto(n, inp)) for n, inp in _tools_of_messages(msgs)]


def _tools_of_messages(msgs):
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        if not isinstance(m, dict):
            continue
        for p in m.get("parts", []):
            if not (isinstance(p, dict) and p.get("type") == "tool"):
                continue
            st = p.get("state") or {}
            nombre = str(p.get("tool") or p.get("name") or st.get("name") or "tool")
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            out.append((nombre, inp))
    return out


def _tool_desc_corto(nombre, inp):
    if isinstance(inp.get("command"), list):
        return " ".join(str(x) for x in inp["command"])[:90]
    if isinstance(inp.get("command"), str):
        return inp["command"][:90]
    for k in ("file_path", "path"):
        if inp.get(k):
            return str(inp[k])
    if inp.get("pattern"):
        return "pattern " + str(inp["pattern"])[:60]
    return ""


def _tool_caja(nombre, inp):
    import difflib
    LOW = 3400
    desc = _tool_desc_corto(nombre, inp)
    head = "🔧 <b>%s</b>%s" % (esc(nombre),
                               (" — <code>%s</code>" % esc(preview(desc, 120)))
                               if desc else "")
    cuerpo = ""
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    if isinstance(cmd, str) and cmd.strip():
        cuerpo = "<pre>%s</pre>" % esc(cmd[:LOW])
        sal = inp.get("output") or inp.get("result") or ""
        if isinstance(sal, str) and sal.strip():
            cuerpo += "\n└ <pre>%s</pre>" % esc(sal[:800])
    elif nombre in ("Edit", "MultiEdit", "Replace") and inp.get("old_string") is not None:
        viejo = str(inp.get("old_string") or "").splitlines()
        nuevo = str(inp.get("new_string") or "").splitlines()
        d = list(difflib.unified_diff(viejo, nuevo, lineterm=""))[2:] or ["(sin cambios)"]
        cuerpo = "<pre>%s</pre>" % esc("\n".join(d)[:LOW])
    elif nombre in ("Write", "Create", "New") and inp.get("content"):
        lineas = str(inp["content"]).splitlines()
        cuerpo = ("<pre>%s</pre>\n<i>(+%d líneas)</i>"
                  % (esc("\n".join(lineas[:40])[:LOW]), len(lineas)))
    elif nombre in ("Read", "View"):
        cuerpo = "<i>leyendo</i> <code>%s</code>" % esc(preview(desc, 160))
    else:
        try:
            cuerpo = "<pre>%s</pre>" % esc(json.dumps(inp, ensure_ascii=False)[:LOW])
        except (TypeError, ValueError):
            cuerpo = ""
    return head + ("\n" + cuerpo if cuerpo else "")


def _cajas_paso_nuevas(cfg, titulo, contador, nuevos):
    if not cfg.get("tools_stream", True):
        return
    for nombre, inp in nuevos:
        contador[0] += 1
        try:
            send(cfg, "%s · <b>%d</b>" % (_tool_caja(nombre, inp),
                                          contador[0]), loud=False)
            if contador[0] % 3 == 0:
                time.sleep(1.0)
        except Exception:
            pass


def _reasoning_of_messages(msgs):
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        if not (isinstance(m, dict) and (m.get("info") or {}).get("role") == "assistant"):
            continue
        for p in m.get("parts", []):
            if isinstance(p, dict) and p.get("type") == "reasoning":
                t = p.get("text") or p.get("reasoning") or ""
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
    return "\n\n".join(out)


def _multipart_body(fields, fname, content):
    import uuid
    b = "----zcb" + uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
                 "%s\r\n" % (b, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; "
             "filename=\"%s\"\r\nContent-Type: text/plain\r\n\r\n"
             % (b, fname)).encode()
    body += content + ("\r\n--%s--\r\n" % b).encode()
    return body, "multipart/form-data; boundary=%s" % b


def send_document(cfg, filename, text, caption=""):
    if not configured(cfg):
        return False
    try:
        body, ctype = _multipart_body(
            {"chat_id": str(cfg["chat_id"]), "caption": caption[:900]},
            fname, text.encode("utf-8"))
        req = urllib.request.Request(
            "%s/bot%s/sendDocument" % (API, cfg.get("token") or ""),
            data=body, headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=90) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as exc:
        log("errors", "sendDocument: %s" % exc)
        return False


def build_trace(titulo, sid, dur_min, msgs, live_cot=""):
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
    try:
        trace = build_trace(titulo, sid, dur_min, msgs, live_cot=live_cot)
    except Exception as exc:
        log("errors", "build_trace: %s" % exc)
        return
    try:
        with open(os.path.join(STATE_DIR, "last-turn-trace.txt"), "w") as f:
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
    send_document(cfg, fname, trace,
                  "🧠 Trace «%s»: %d pasos · CoT %s chars · respuesta aparte"
                  % (titulo or "?", np, fmt_tok(ncot)))


def _send_long_reply(cfg, titulo, dur_min, texto, max_partes=3):
    CH = 3900
    total = len(texto)
    partes = [texto[i:i + CH] for i in range(0, total, CH)]
    try:
        with open(os.path.join(BASE, "hooks", "state", "last-reply.txt"), "w") as f:
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
        pre = ("✅ «%s» listo en %.0f min" % (esc(titulo), dur_min)) if i == 0 else "···"
        suf = " (%d/%d)" % (i + 1, len(partes)) if len(partes) > 1 else ""
        send(cfg, pre + suf + "\n" + esc(p) + (cola if i == len(partes) - 1 else ""),
             loud=False)


import os
import re
import sys
import time
import json
import subprocess
