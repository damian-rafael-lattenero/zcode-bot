# -*- coding: utf-8 -*-
"""commands.queues — comandos de colas: /queue, /qgo, /qdel."""
from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

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


def cmd_queue(cfg, rest):
    """/queue — el CONTENIDO de las colas (F22): cada encolado con botones
    ⏫ (lanzar ahora) y 🗑 (descartar)."""
    sids = _queue_sids_with_items()
    if not sids:
        return "📭 No hay mensajes encolados."
    lines = ["📬 <b>Cola de prompts</b>"]
    kb = []
    for sid in sids:
        act_sid = (active_bot_session() or {}).get("sid")
        marca = " 🎯" if sid == act_sid else ""
        titulo = session_title_from_db(sid) or sid[-8:]
        lines.append("«%s»%s:" % (esc(titulo), marca))
        for i, item in enumerate(_queue_peek(sid), 1):
            lines.append("  %d. %s" % (i, esc(preview(item.get("text", ""), 80))))
            cb = ("%s:%s:%d" % ("x", sid, i - 1))  # base comun
            kb.append([
                {"text": "⏫ %d ahora" % i, "callback_data": "qgo:%s:%d" % (sid, i - 1)},
                {"text": "🗑", "callback_data": "qdel:%s:%d" % (sid, i - 1)}])
    return "\n".join(lines), {"inline_keyboard": kb}


def _queue_mutate(sid, fn):
    """F22: read-modify-write ATÓMICO de UNA cola del dict (no pisa las
    demás sesiones). fn(lst) -> nueva lst (o None = sin cambios)."""

    def apply(q):
        q = q if isinstance(q, dict) else {}
        nueva = fn(list(q.get(sid) or []))
        if nueva is None:
            return None
        if nueva:
            q[sid] = nueva
        else:
            q.pop(sid, None)
        return q
    try:
        _store("bot-queue.json").update_cross_process(apply, {})
    except OSError:
        pass


def cmd_qgo(cfg, rest):
    """/qgo <sid>:<idx> — lanza YA ese encolado (lo saca de la cola)."""
    try:
        sid, idx = rest.rsplit(":", 1)
        idx = int(idx)
    except ValueError:
        return "⚠️ /qgo sid:idx"
    lst = _queue_peek(sid)
    if not (0 <= idx < len(lst)):
        return "🤔 Ese encolado ya no está (¿lo lanzaste dos veces?)."
    item = lst[idx]
    if WORKERS.get(sid) is not None or (sid in running_turns()):
        return "⏳ «%s» sigue ocupada — el turno en curso la drena al final." \
            % esc(sid[-8:])
    taken = [[]]

    def take(l):
        taken[0] = l[idx:idx + 1]
        return l[:idx] + l[idx + 1:]
    _queue_mutate(sid, take)
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        _queue_put(sid, item["act"], item["text"])  # no perderlo
        return None
    return "⏫ Lanzado: %s" % esc(preview(item.get("text", ""), 100))


def cmd_qdel(cfg, rest):
    """/qdel <sid>:<idx> — descarta ese encolado."""
    try:
        sid, idx = rest.rsplit(":", 1)
        idx = int(idx)
    except ValueError:
        return "⚠️ /qdel sid:idx"
    item = [[]]

    def drop(l):
        if not (0 <= idx < len(l)):
            return None
        item[0] = l[idx]
        return l[:idx] + l[idx + 1:]
    _queue_mutate(sid, drop)
    if not item[0]:
        return "🤔 Ese encolado ya no está."
    return "🗑 Descartado: %s" % esc(preview(item[0].get("text", ""), 100))


def _queue_mutate(sid, fn):
    """F22: read-modify-write ATÓMICO de UNA cola del dict (no pisa las
    demás sesiones). fn(lst) -> nueva lst (o None = sin cambios)."""

    def apply(q):
        q = q if isinstance(q, dict) else {}
        nueva = fn(list(q.get(sid) or []))
        if nueva is None:
            return None
        if nueva:
            q[sid] = nueva
        else:
            q.pop(sid, None)
        return q
    try:
        _store("bot-queue.json").update_cross_process(apply, {})
    except OSError:
        pass


def _queue_put(sid, act, text):
    def add(q):
        q = q if isinstance(q, dict) else {}
        lst = q.get(sid) or []
        lst.append({"act": act, "text": text[:6000]})
        q[sid] = lst[-5:]
        return q
    try:
        # F16: flock — poller y workers-proceso escriben la misma cola
        _store("bot-queue.json").update_cross_process(add, {})
    except OSError:
        pass


def _queue_take(sid):
    taken = [[]]

    def take(q):
        q = q if isinstance(q, dict) else {}
        taken[0] = q.pop(sid, [])
        return q
    try:
        _store("bot-queue.json").update_cross_process(take, {})
    except OSError:
        pass
    return taken[0]


def _queue_peek(sid):
    q = _store("bot-queue.json").read()
    return (q.get(sid) or []) if isinstance(q, dict) else []


def _queue_sids_with_items():
    q = _store("bot-queue.json").read()
    if not isinstance(q, dict):
        return []
    return [sid for sid, lst in q.items() if lst]


import os
import json
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
import time
import subprocess
import threading

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


def _queue_put(sid, act, text):
    def add(q):
        q = q if isinstance(q, dict) else {}
        lst = q.get(sid) or []
        lst.append({"act": act, "text": text[:6000]})
        q[sid] = lst[-5:]
        return q
    try:
        # F16: flock — poller y workers-proceso escriben la misma cola
        _store("bot-queue.json").update_cross_process(add, {})
    except OSError:
        pass


def _queue_take(sid):
    taken = [[]]

    def take(q):
        q = q if isinstance(q, dict) else {}
        taken[0] = q.pop(sid, [])
        return q
    try:
        _store("bot-queue.json").update_cross_process(take, {})
    except OSError:
        pass
    return taken[0]


def _queue_peek(sid):
    q = _store("bot-queue.json").read()
    return (q.get(sid) or []) if isinstance(q, dict) else []


def _queue_sids_with_items():
    q = _store("bot-queue.json").read()
    if not isinstance(q, dict):
        return []
    return [sid for sid, lst in q.items() if lst]


def _queue_mutate(sid, fn):
    """F22: read-modify-write ATÓMICO de UNA cola del dict (no pisa las
    demás sesiones). fn(lst) -> nueva lst (o None = sin cambios)."""

    def apply(q):
        q = q if isinstance(q, dict) else {}
        nueva = fn(list(q.get(sid) or []))
        if nueva is None:
            return None
        if nueva:
            q[sid] = nueva
        else:
            q.pop(sid, None)
        return q
    try:
        _store("bot-queue.json").update_cross_process(apply, {})
    except OSError:
        pass


import os
import json
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
import time
import subprocess
import threading

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