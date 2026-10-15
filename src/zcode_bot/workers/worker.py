"""workers.worker — worker actor (proceso separado) para turnos headless.

Patrón: cada worker es un proceso con estado privado (SU runtime, SUS
pipes); el poller es supervisor; TODA comunicación es por mensajes en
state/ (bot-workers.json = registry, bot-stop/<sid> = orden de stop,
bot-q/ = canal de preguntas). El receive es REACTIVO (kqueue: el thread
duerme en el kernel y despierta con el mensaje — cero CPU, sin sondeos).
"""
import asyncio
import json
import os
import re
import sys
import time
import threading

from zcode_bot.utils.config import load_config, _nuclear_active, _save_last_prompt
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send
from zcode_bot.utils.logging import log, pwlog

from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles
from zcode_bot.hooks.questions import _user_input_handler
from zcode_bot.state.manager import _stop_request, _stop_consume


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


def cmd_run_turn():
    """F16: entrypoint del worker-proceso (--run-turn). Lee {act, text} de
    stdin, instala el SIGTERM → marker (nuclear amable: el shutdown pasa
    por el mismo camino que /stop) y un atexit de seguridad para el
    registry; luego corre el mismo prompt_worker de siempre."""
    import atexit
    import signal
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    act = payload.get("act") or {}
    text = payload.get("text") or ""
    sid = act.get("sid")

    def _sig(signum, frame):
        _stop_request(sid)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (OSError, ValueError):
        pass
    atexit.register(lambda: WORKERS.pop(sid or "nueva"))
    prompt_worker(load_config(), act, text)


def _stop_request(sid):
    """/stop: el poller deja la orden; el worker la ve en su loop (~2 s)
    y frena SU turno con su propio runtime (el 'stop real': solo el
    runtime dueño ve sus turnos — el ProtoClient compartido murió con los
    threads)."""
    try:
        with open(_stop_file(sid), "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _stop_file(sid):
    d = os.path.join(STATE_DIR, "bot-stop")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sid or "ses")
    return os.path.join(d, safe)


def _stop_requested(sid):
    return bool(sid) and os.path.exists(_stop_file(sid))


def _stop_consume(sid):
    try:
        os.remove(_stop_file(sid))
    except OSError:
        pass


import os
import re
import sys
import time
import json
import subprocess

from zcode_bot.utils.config import load_config, _nuclear_active, _save_last_prompt
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send
from zcode_bot.utils.logging import log, pwlog

from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles
from zcode_bot.hooks.questions import _user_input_handler
from zcode_bot.state.manager import _stop_request, _stop_consume