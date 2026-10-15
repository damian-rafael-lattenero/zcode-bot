"""commands.control — comandos de control: /stop, /nuclear, /deploy, /retry, /status, /quota, /usage."""
from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document

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


def cmd_stop(cfg, rest):
    """/stop SOLO maneja corridas iniciadas desde el celular: lo que corre
    en la Mac se frena con el botón Stop de la app, acá ni se lista."""
    if rest.startswith("@ses"):
        return bot_stop(rest.split()[0][1:])
    if rest.isdigit():
        items = WORKERS.items()
        k = int(rest)
        if 1 <= k <= len(items):
            sid, w = items[k - 1]
            return bot_stop(sid, w.get("titulo"))
        return "⚠️ Número fuera de la lista (1 a %d) — mandá /stop." % len(items)
    return stop_list_report()


def cmd_nuclear(cfg, rest):
    """/nuclear — EMERGENCIA: frena TODO (corridas del celular + tasks de la
    UI de la Mac) y reinicia la app y el bot. No hay entrada externa al
    runtime de la app (ADR-006): cerrar y reabrir ES el mecanismo. Solo se
    ejecuta con 'go' — que solo produce el botón de confirmación."""
    r = (rest or "").strip().lower()
    if r == "go":
        import threading as _th
        _th.Thread(target=_nuclear_run, args=(cfg,), daemon=True).start()
        return ("☢️ Nuclear 2.0 arrancando — pre-vuelo te dice qué se frena, "
                "qué se pierde y qué se conserva. El veredicto final llega "
                "verificado (watchdog post-reinicio).")
    if r == "no":
        return "🙅 Cancelado — no se tocó nada."
    return ("☢️ <b>Nuclear</b> — emergencia:\n"
            "⏹️ frena TODAS las corridas (celular + Mac)\n"
            "🖥️ cierra y reabre la app ZCode\n"
            "🔁 reinicia el bot\n\n¿Confirmás?",
            {"inline_keyboard": [[
                {"text": "☢️ Confirmar", "callback_data": "nuc:go"},
                {"text": "🙅 Cancelar", "callback_data": "nuc:no"}]]})


def cmd_deploy(cfg, rest):
    """/deploy — el pipeline completo desde el celular (F25): tests →
    backup → copia → recarga instantánea. Confirmación en dos pasos
    (el 'go' solo lo produce el botón). Solo tiene sentido si HAY cambios
    que deployar: corre deploy.sh tal cual."""
    r = (rest or "").strip().lower()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.dirname(os.path.abspath(__file__))
    deploy_sh = os.path.join(os.path.dirname(src), "deploy.sh")
    if not os.path.exists(deploy_sh):
        # en producción (~/.zcode/hooks) el repo vive en el workspace del bot
        for cand in ("/Users/lattenero/Projects/bot/deploy.sh",):
            if os.path.exists(cand):
                deploy_sh = cand
                break
    if r == "go":
        def run():
            try:
                p = subprocess.run(["bash", deploy_sh], capture_output=True,
                                   text=True, timeout=600, cwd=os.path.dirname(deploy_sh))
                tail = (p.stdout or "").strip().splitlines()[-6:]
                ok = p.returncode == 0
                send(cfg, ("✅ <b>Deploy OK</b>\n<pre>%s</pre>" % esc("\n".join(tail)[-1500:]))
                     if ok else
                     ("❌ <b>Deploy falló</b> (rc=%d)\n<pre>%s</pre>"
                      % (p.returncode, esc("\n".join(tail)[-1200:]))),
                     loud=not ok)
            except Exception as exc:
                send(cfg, "❌ deploy: %s" % esc(str(exc)[:150]), loud=True)
        import threading as _th
        _th.Thread(target=run, daemon=True).start()
        return "🚀 Deploy corriendo (tests incluidos, ~1 min) — te aviso."
    if r == "no":
        return "🙅 Cancelado."
    return ("🚀 <b>Deploy</b> — tests → backup → copia → recarga "
            "instantánea del poller. ¿Corro deploy.sh?",
            {"inline_keyboard": [[
                {"text": "🚀 Deploy", "callback_data": "dep:go"},
                {"text": "🙅 No", "callback_data": "dep:no"}]]})


def cmd_retry(cfg, rest):
    """/retry — relanza el último prompt tal cual (fallo, crash o timeout).
    Si hay una task activa distinta a la del prompt, pregunta... no: corre
    en la task ORIGINAL del prompt (donde tiene contexto)."""
    act, text = _load_last_prompt()
    if not text:
        return "🤔 No tengo un prompt previo para reintentar."
    if WORKERS.get((act or {}).get("sid") or "nueva") is not None:
        return "⏳ Ya hay un turno corriendo en «%s» — /stop primero si querés." % esc((act or {}).get("title", "?"))
    if _spawn_worker(cfg, act, text) is None:
        return None
    return "🔁 Reintentando «%s»:\n%s" % (esc((act or {}).get("title", "?")),
                                           esc(preview(text, 200)))


def cmd_status(cfg, rest):
    from zcode_bot.gateway import status_report
    return status_report()


def cmd_quota(cfg, rest):
    from zcode_bot.gateway import quota_report
    return quota_report()


def cmd_usage(cfg, rest):
    from zcode_bot.gateway import usage_report
    return usage_report()


def cmd_retry(cfg, rest):
    """/retry — relanza el último prompt tal cual (fallo, crash o timeout).
    Si hay una task activa distinta a la del prompt, pregunta... no: corre
    en la task ORIGINAL del prompt (donde tiene contexto)."""
    act, text = _load_last_prompt()
    if not text:
        return "🤔 No tengo un prompt previo para reintentar."
    if WORKERS.get((act or {}).get("sid") or "nueva") is not None:
        return "⏳ Ya hay un turno corriendo en «%s» — /stop primero si querés." % esc((act or {}).get("title", "?"))
    if _spawn_worker(cfg, act, text) is None:
        return None
    return "🔁 Reintentando «%s»:\n%s" % (esc((act or {}).get("title", "?")),
                                           esc(preview(text, 200)))


def _load_last_prompt():
    try:
        with open(os.path.join(STATE_DIR, "last-prompt.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None, None
    if isinstance(d, dict) and d.get("text"):
        return d.get("act") or {}, str(d["text"])
    return None, None


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


def cmd_stop(cfg, rest):
    """/stop SOLO maneja corridas iniciadas desde el celular: lo que corre
    en la Mac se frena con el botón Stop de la app, acá ni se lista."""
    if rest.startswith("@ses"):
        return bot_stop(rest.split()[0][1:])
    if rest.isdigit():
        items = WORKERS.items()
        k = int(rest)
        if 1 <= k <= len(items):
            sid, w = items[k - 1]
            return bot_stop(sid, w.get("titulo"))
        return "⚠️ Número fuera de la lista (1 a %d) — mandá /stop." % len(items)
    return stop_list_report()


def bot_stop(sid, titulo=None):
    """Frena una corrida iniciada DESDE el celular Y limpia su cola
    (pedido F17: /stop también descarta los mensajes encolados de esa
    task). F16: el frenado va por marker — el WORKER lo ve en su loop y
    ejecuta session/stop con su propio runtime (stop real)."""
    n = len(_queue_take(sid))
    tit = WORKERS.flag_stop(sid)
    if tit is None:
        if n:
            return ("🧹 Limpié la cola de «%s» (%d mensajes) — no había "
                    "corrida del bot que frenar."
                    % (esc(titulo or sid[-8:]), n))
        return ("🤔 Esa corrida ya no existe (terminó o no fue iniciada "
                "desde el celular).")
    _stop_request(sid)
    extra = (" · 🧹 cola limpiada (%d)" % n) if n else ""
    return "🛑 Frenando «%s»…%s" % (esc(tit or titulo or sid[-8:]), extra)


def stop_list_report():
    """/stop → corridas del celular + colas dormidas (Template Method)."""
    items = WORKERS.items()
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
              if WORKERS.get(sid) is None]

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
        lambda *a: "", lambda *a: None)


def cmd_quota(cfg, rest):
    from zcode_bot.gateway import quota_report
    return quota_report()


def cmd_usage(cfg, rest):
    from zcode_bot.gateway import usage_report
    return usage_report()


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