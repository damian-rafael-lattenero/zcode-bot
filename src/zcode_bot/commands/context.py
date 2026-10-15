"""commands.context — comandos de contexto: /ctx, /compact, /cot."""
from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import sys
import datetime
import subprocess
import threading

from zcode_bot import tasks_repo as zc_repo
from zcode_bot import reports as zc_reports


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


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
        target = {t["sid"]: t for t in ctx_tasks(30)}.get(sid)
        if target is None:
            return "⚠️ No encontré esa conversación — mandá /task."
    elif n is not None:
        tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
        if n < 1 or n > len(tasks):
            return "⚠️ Número fuera de la lista (1 a %d) — mandá /compact." % len(tasks)
        target = tasks[n - 1]
    else:
        act = active_bot_session()
        target = None
        if act and act.get("sid"):
            target = {t["sid"]: t for t in ctx_tasks(30)}.get(act["sid"])
        if target is None:
            return "⚠️ Elegí conversación primero (/task n) o pasá su número."
    sid, ws, titulo = target["sid"], target["ws"], target["title"]
    try:
        c = zc_proto.make(cwd=ws)
    except OSError as e:
        return "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120])
    snap_sel = None
    try:
        r = c.request("session/resume", {"sessionId": sid}, timeout=60)
        if "error" in r:
            return "❌ resume: " + esc(json.dumps(r["error"])[:150])
        st = {x["sessionId"]: x.get("status")
              for x in (r.get("result") or {}).get("sessions", [])}
        if st.get(sid) == "running":
            return ("⚠️ «%s» está corriendo — frená primero (/stop o el botón "
                    "Stop) y reintentá." % esc(titulo))
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": MODEL_SEL}, timeout=30)
        if "error" in r:
            return "❌ setModel: " + esc(json.dumps(r["error"])[:150])
        snap_sel = _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": MODEL_SEL}, timeout=30)
        if "error" in r:
            return "❌ setModel: " + esc(json.dumps(r["error"])[:150])
        snap_sel = _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot
        params = {"sessionId": sid}
        if rest:
            params["instructions"] = rest
        started = _t.time()
        r = c.request("session/compact", params, timeout=300)
        if "error" in r:
            return "❌ compact: " + esc(json.dumps(r["error"])[:150])
        fin = _wait_cli_event(sid, started,
                              ("compact.completed", "compact.failed"), 200)
        if fin is None:
            return ("🤔 Compactación enviada pero no vi el evento de fin en "
                    "200 s. Verificá con /ctx.")
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
        if sid and snap_sel:
            try:
                _restore_model(sid, snap_sel)
            except Exception as e:
                log("errors", "worker restore: %s" % e)


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


MODEL_SEL = {"providerId": "zai-bot", "modelId": "GLM-5.3",
             "options": {"reasoningLevel": "high"}}
THINKING_LEVELS = ("max", "high", "low")
MODEL_THINKING = {
    "GLM-5.3": ("max", "high", "low"),
    "GLM-5.3-Flash": ("max", "high", "low"),
}


def _thinking_levels(model_id):
    return MODEL_THINKING.get(model_id, THINKING_LEVELS)


MODELOS = ("GLM-5.3", "GLM-5.3-Flash")


def _cur_sel(act):
    """Model selection vigente de la conversación activa (dict sane con
    provider/modelId/options.reasoningLevel)."""
    sel = dict(MODEL_SEL)
    sel["options"] = dict(MODEL_SEL.get("options") or {})
    if isinstance(act.get("model"), dict):
        sel.update({k: v for k, v in act["model"].items() if k != "options"})
        sel["options"] = dict(act["model"].get("options") or sel["options"])
    return sel


def _set_sel(act, model_id=None, thinking=None):
    """Cambia modelo o thinking de la activa PRESERVANDO lo otro."""
    sel = _cur_sel(act)
    if model_id:
        sel["modelId"] = model_id
    if thinking:
        sel["options"] = dict(sel.get("options") or {}, reasoningLevel=thinking)
    set_active_bot_session(dict(act, model=sel))


def cmd_compact(cfg, rest):
    if not rest:
        return compact_list_report("projects")
    return _compact_args(rest, "projects")


def cmd_all_compact(cfg, rest):
    if not rest:
        return compact_list_report("all")
    return _compact_args(rest, "all")


def cmd_cot(cfg, rest):
    """/cot [n|@sid] — paso a paso + razonamiento completo de una task como
    documento scrolleable. Sin args: la activa. Funciona TAMBIÉN para tasks
    corridas en la app de la Mac (leer el historial sí cruza runtimes —
    lo que no cruza es frenar)."""
    import datetime
    tasks = {t["sid"]: t for t in ctx_tasks(30)}
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
        if "error" in rr:
            return "❌ resume: " + esc(json.dumps(rr["error"])[:150])
        msgs = _fetch_messages(c, target["sid"], limit=150) or []
        trace = build_trace(target["title"] or target["sid"][-8:],
                            target["sid"], 0.0, msgs)
        try:
            with open(os.path.join(STATE_DIR, "last-turn-trace.txt"), "w") as f:
                f.write(trace)
        except OSError:
            pass
        fname = "cot-%s-%s.txt" % (
            re.sub(r"[^A-Za-z0-9._-]+", "-",
                   (target["title"] or "ses")[:24]).strip("-"),
            datetime.datetime.now().strftime("%H%M"))
        ok = send_document(cfg, fname, trace,
                           "🧠 Trace de «%s»: %d pasos · últimos %d mensajes"
                           % (target["title"] or target["sid"][-8:],
                              len(_steps_of_messages(msgs)), len(msgs)))
        return ("📄 Documento enviado." if ok else
                "⚠️ No pude mandar el documento — reintentá /cot.")
    finally:
        c.close()


import os
import json
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles