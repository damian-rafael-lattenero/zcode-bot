"""commands.tasks — comandos de tareas: /task, /all_tasks, /run."""
import os
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import reports as zc_reports
from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

import os
import sys
import threading
import subprocess
import time

BASE = os.path.join(os.path.expanduser("~"), ".zcode")
STATE_DIR = os.path.join(BASE, "hooks", "state")
LOG_DIR = os.path.join(BASE, "hooks", "logs")
DEFAULT_WS = os.path.join(BASE, "workspace", "default")
API = os.environ.get("ZT_API_BASE", "https://api.telegram.org")


def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


def _resolve_ws(tag):
    """Resuelve un tag de proyecto ('bot', 'cv', haskell...) a path."""
    act = active_workspace_path()
    if not tag:
        return act
    tagl = tag.strip().strip("/").lower()
    cands = set([act]) | {t["ws"] for t in ctx_tasks(20) if t.get("ws")}
    for w in cands:
        if w and os.path.basename(w).lower() == tagl:
            return w
    for w in cands:
        if w and tagl in os.path.basename(w).lower():
            return w
    return None


def _tasks_scope(scope, limit):
    """/task → solo tasks con Project (excluye las sueltas del workspace
    default); /all_tasks → todas bajo el grupo Tasks."""
    tasks = ctx_tasks(limit)
    if scope != "all":
        tasks = [t for t in tasks if t["ws"] != DEFAULT_WS]
    for i, t in enumerate(tasks, 1):
        t["n"] = i
    return tasks


def ctx_tasks(limit=10):
    """Tasks con consumo real, sin archivadas, en el orden de la barra
    lateral de la app (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.ctx_tasks(BASE, limit=limit)


def task_list_report(scope="projects"):
    """/task → lista tocable (Template Method: zcode_bot.reports)."""
    tasks = _tasks_scope("all" if scope == "all" else "projects", 20 if scope == "all" else 10)
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
        "🧠 Sin tasks%s para compactar — /all_tasks muestra todas."
        % ("" if scope == "all" else " de proyecto"),
        line_of, button_of,
        "Texto libre que mandes va a la conversación activa (🎯).")


def _task_select_sid(sid, titulo=None):
    """Fija la conversación activa por sessionId (los botones la usan: la
    posición numérica puede repetirse entre workspaces, el sid no)."""
    tasks = {t["sid"]: t for t in ctx_tasks(30)}
    t = tasks.get(sid)
    if not t:
        return "⚠️ No encontré esa conversación — mandá /task."
    prev = active_bot_session() or {}
    nueva = {"sid": t["sid"], "title": t["title"] or t["sid"][-8:],
             "ws": t["ws"]}
    # F22: la config del dueño SOBREVIVE el switch (antes /model, /thinking
    # y /mode se perdían al cambiar de conversación)
    for k in ("model", "mode"):
        if prev.get(k):
            nueva[k] = prev[k]
    set_active_bot_session(nueva)
    activos = running_turns()
    extra = " (⚠️ está corriendo un turno ahora)" if t["sid"] in activos else ""
    reply = ("🎯 Conversación activa: «%s»%s\nTodo texto que mandes va ahí "
             "(corre headless — sirve con la pantalla bloqueada)."
             % (esc(t["title"] or t["sid"][-8:]), extra))
    resume = _queue_resume(load_config(), t["sid"], t["title"])  # F15: cola dormida
    if resume:
        reply += "\n" + resume
    return reply


def task_select(n, scope="projects"):
    tasks = _tasks_scope(scope, 20 if scope == "all" else 10)
    if n < 1 or n > len(tasks):
        return "⚠️ Número fuera de la lista (1 a %d) — mandá /task." % len(tasks)
    t = tasks[n - 1]
    return _task_select_sid(t["sid"], t["title"])


def cmd_task(cfg, rest):
    if not rest:
        return task_list_report("projects")
    if rest.startswith("@ses"):
        return _task_select_sid(rest[1:])
    if rest.isdigit():
        return task_select(int(rest), "projects")
    if rest.split()[0] == "new":
        tag = rest.split(" ", 1)[1].strip() if " " in rest else ""
        ws = _resolve_ws(tag)
        if not ws:
            return ("⚠️ No conozco el proyecto «%s» — probá sin nombre "
                    "(usa el activo) o el nombre de carpeta." % esc(tag))
        set_active_bot_session({"sid": None, "ws": ws,
                                "title": "nueva · " + os.path.basename(ws)})
        return ("🆕 Nueva conversación en %s — mandá el primer mensaje "
                "y arranca (headless)." % esc(os.path.basename(ws)))
    return ("⚠️ /task lista · /task <n> elige · /task new [proyecto] · "
            "/all_tasks muestra todas")


def cmd_all_tasks(cfg, rest):
    if rest.isdigit():
        return task_select(int(rest), "all")
    return task_list_report("all")


def cmd_run(cfg, rest):
    import threading as _th
    if not rest:
        return "⚠️ /run [proyecto] <prompt> — crea una conversación nueva y manda el prompt."
    partes = rest.split(" ", 1)
    if len(partes) == 2 and _resolve_ws(partes[0]) and " " in rest:
        ws, prompt = _resolve_ws(partes[0]), partes[1].strip()
    else:
        ws, prompt = active_workspace_path(), rest
    if not prompt:
        return "⚠️ Falta el prompt."
    act = {"sid": None, "ws": ws, "title": "run · " + os.path.basename(ws or "")}
    _spawn_worker(cfg, act, prompt)
    return None


def _resolve_ws(tag):
    """Resuelve un tag de proyecto ('bot', 'cv', haskell...) a path."""
    act = active_workspace_path()
    if not tag:
        return act
    tagl = tag.strip().strip("/").lower()
    cands = set([act]) | {t["ws"] for t in ctx_tasks(20) if t.get("ws")}
    for w in cands:
        if w and os.path.basename(w).lower() == tagl:
            return w
    for w in cands:
        if w and tagl in os.path.basename(w).lower():
            return w
    return None


def _tasks_scope(scope, limit):
    """/task → solo tasks con Project (excluye las sueltas del workspace
    default); /all_tasks → todas bajo el grupo Tasks."""
    tasks = ctx_tasks(limit)
    if scope != "all":
        tasks = [t for t in tasks if t["ws"] != DEFAULT_WS]
    for i, t in enumerate(tasks, 1):
        t["n"] = i
    return tasks


def ctx_tasks(limit=10):
    """Tasks con consumo real, sin archivadas, en el orden de la barra
    lateral de la app (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.ctx_tasks(BASE, limit=limit)


def _spawn_worker(cfg, act, text):
    """F16: lanza el worker como PROCESO detached (start_new_session=True,
    patrón del stuck-watch). Un kickstart/restart/crash del poller NO lo
    toca: el turno vive su ciclo completo y se reporta solo por Telegram.
    Payload JSON por stdin (argv se queda corto para prompts de 6KB)."""
    if _nuclear_active():
        # Nuclear 2.0 — cerradura: NADA nuevo arranca durante el ciclo
        # (antes el poller podía spawnear un worker a mitad del nuclear
        # que moría con el kickstart sin reporte). La cola queda dormida
        # y sobrevive: el auto-resume la drena al volver.
        pwlog("spawn bloqueado: nuclear en curso")
        return None
    _save_last_prompt(act, text)  # F22: /retry siempre tiene qué relanzar
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        with open(os.path.join(LOG_DIR, "worker-stderr.log"), "a") as err:
            p = subprocess.Popen(
                [sys.executable, me, "--run-turn"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                start_new_session=True,
                cwd=act.get("ws") or os.path.expanduser("~"))
            p.stdin.write(json.dumps({"act": act, "text": text}).encode("utf-8"))
            p.stdin.close()
        return p
    except OSError as e:
        send(cfg, "❌ No pude lanzar el worker: " + esc(str(e)[:120]))
        return None


import os
import sys
import time
import subprocess
import threading

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import reports as zc_reports
from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

import os
import sys
import threading
import subprocess
import time

BASE = os.path.join(os.path.expanduser("~"), ".zcode")
STATE_DIR = os.path.join(BASE, "hooks", "state")
LOG_DIR = os.path.join(BASE, "hooks", "logs")
DEFAULT_WS = os.path.join(BASE, "workspace", "default")
API = os.environ.get("ZT_API_BASE", "https://api.telegram.org")