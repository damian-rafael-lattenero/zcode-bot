"""commands.dispatch — el despacho del 2_bot (F4i).

wait_reply (la espera de respuesta del dueño), la tabla Command Registry
con los ~25 handlers y los dos puntos de entrada (handle_command para
"/comandos", handle_free_text para texto libre → conversación activa).

F9 (cierre de la deuda ADR-012): los handlers viven por dominio en
handlers_tareas (conversaciones/cola), handlers_info (informes/menú) y
handlers_sistema (control/modelo/webhook) — movidos TEXTUALES, la tabla
de acá solo los registra. Este módulo conserva wait_reply (la espera
compartida con hooks) y la superficie re-exportada que consume el
gateway. Transporte POR ATRIBUTO (uhttp.*).
"""
import os
import threading
import time

from zcode_bot import commands as zc_commands
from zcode_bot import config
from zcode_bot.core import WaitReply
from zcode_bot.updates import (drain_updates, save_offset, busy_path,
                               fetch_updates, from_owner)
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.validation import classify
from zcode_bot.utils.logging import log

# handlers por dominio (F9) — re-exportados: la facade (gateway) importa
# estos nombres de acá, igual que desde F4i
from zcode_bot.commands.registry import CommandSpec
from zcode_bot.commands.handlers_tareas import (  # noqa: F401
    task_select, _task_select_sid, handle_free_text, _cmd_retry, _cmd_stop,
    _cmd_task, _cmd_all_tasks, _cmd_queue, _queue_mutate, _cmd_qgo,
    _cmd_qdel, _cmd_run)
from zcode_bot.commands.handlers_info import (  # noqa: F401
    register_bot_menu, _cmd_quota, _cmd_usage, _cmd_ctx, _cmd_compact,
    _cmd_all_compact, _cmd_status, _cmd_last, _cmd_cot, _cmd_help,
    _cmd_diff, _cmd_cotb)
from zcode_bot.commands.handlers_sistema import (  # noqa: F401
    _cmd_start, _cmd_deploy, _cmd_nuclear, _cmd_ping, _cmd_model,
    _cmd_thinking, _cmd_mode, _cmd_fork, _cmd_api, _cmd_set_webhook,
    _cmd_delete_webhook, _cmd_webhook_info)


def wait_reply(cfg, hint):
    """Espera una respuesta NUEVA por Telegram hasta wait_timeout_s.

    Devuelve ("yes"|"no"|"text", texto) o (None, None) si se agotó el tiempo.
    Los mensajes que empiezan con "/" son comandos del 2_bot: se responden y
    la espera continúa (no cuentan como respuesta).
    """
    deadline = time.time() + int(cfg.get("wait_timeout_s"))
    offset, stale = drain_updates(cfg)  # lo pendiente no cuenta como respuesta
    save_offset(offset)
    os.makedirs(config.STATE_DIR, exist_ok=True)
    try:
        while time.time() < deadline:
            # flag para el poller de comandos: no consumir getUpdates mientras esperamos
            with open(busy_path(), "w") as f:
                f.write(str(time.time()))
            remaining = int(deadline - time.time())
            poll_timeout = max(1, min(25, remaining))
            try:
                offset, batch, _ = fetch_updates(cfg, offset, poll_timeout)
                save_offset(offset)
            except Exception as exc:  # noqa: BLE001
                log("errors", f"getUpdates (wait) falló: {exc}")
                # Sin sleep(3): el long-poll timeout ya hizo el wait
                continue
            for msg in batch:
                if not from_owner(cfg, msg):
                    continue
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    reply = handle_command(cfg, text)
                    if reply is not None:
                        if isinstance(reply, tuple):
                            uhttp.send(cfg, reply[0], loud=False, markup=reply[1])
                        else:
                            uhttp.send(cfg, reply, loud=False)
                        continue
                kind = classify(text)
                return WaitReply(kind if kind in ("yes", "no") else "text", text)
            if not batch:
                # Sin sleep(0.5): el long-poll timeout=25 ya bloquea en el server
                pass
        return WaitReply(None, None)
    finally:
        try:
            os.remove(busy_path())
        except OSError:
            pass


REG = zc_commands.CommandRegistry()  # despacho por tabla (Command Registry)

# R6 — LA tabla de comandos: de acá salen el REG, los SLOW_CMDS y el menú
# de Telegram (los visibles van PRIMERO y en el orden del menú; el orden
# de registro es irrelevante para el despacho). El /help es texto curado
# (líneas agrupadas a mano) pero test_help_cubre_specs exige que todo
# comando visible aparezca en él. menu_desc vacío = oculto del menú
# (callbacks internos: qgo/qdel; bootstrap: start; admin: webhook/deploy).
SPECS = [
    CommandSpec(("task",), _cmd_task,
                menu_desc="conversaciones con Project · /task n elige la activa"),
    CommandSpec(("all_tasks", "alltasks", "tasks"), _cmd_all_tasks,
                menu_desc="todas las conversaciones, incluidas las sueltas"),
    CommandSpec(("model",), _cmd_model,
                menu_desc="modelo de la conversación activa (botones)"),
    CommandSpec(("thinking",), _cmd_thinking,
                menu_desc="nivel de razonamiento: max · high · low"),
    CommandSpec(("mode",), _cmd_mode,
                menu_desc="Plan mode 🗺 · Full access 🔓 (botones)"),
    CommandSpec(("status",), _cmd_status,
                menu_desc="qué está corriendo + conversación activa"),
    CommandSpec(("diff",), _cmd_diff,
                menu_desc="trabajo en curso del workspace activo · /diff log = progreso · /diff commit"),
    CommandSpec(("queue", "cola"), _cmd_queue,
                menu_desc="mensajes en cola (lanzar ahora / descartar)"),
    CommandSpec(("retry",), _cmd_retry,
                menu_desc="relanzar el último prompt (tras un fallo)"),
    CommandSpec(("ctx", "contexto"), _cmd_ctx,
                menu_desc="contexto usado por cada task", slow=True),
    CommandSpec(("compact",), _cmd_compact,
                menu_desc="elegir task para compactar (Projects)", slow=True),
    CommandSpec(("all_compact", "allcompact"), _cmd_all_compact,
                menu_desc="ídem, incluidas las sueltas del grupo Tasks",
                slow=True),
    CommandSpec(("run",), _cmd_run,
                menu_desc="nueva tarea y mandar prompt (/run proyecto prompt)"),
    CommandSpec(("stop", "all_stop", "allstop"), _cmd_stop,
                menu_desc="frena la corrida y LIMPIA su cola de mensajes",
                slow=True),
    CommandSpec(("nuclear", "nuke"), _cmd_nuclear,
                menu_desc="emergencia: frena TODO (Mac incluida) y reinicia"),
    CommandSpec(("quota",), _cmd_quota, menu_desc="credits del coding plan",
                slow=True),
    CommandSpec(("usage", "uso"), _cmd_usage,
                menu_desc="consumo de hoy por sesión", slow=True),
    CommandSpec(("last",), _cmd_last,
                menu_desc="reenviar la última respuesta completa"),
    CommandSpec(("cot", "trace"), _cmd_cot,
                menu_desc="paso a paso + razonamiento del turno (documento)"),
    CommandSpec(("help",), _cmd_help, menu_desc="todos los comandos"),
    # — ocultos del menú —
    CommandSpec(("start",), _cmd_start),
    CommandSpec(("deploy",), _cmd_deploy),
    CommandSpec(("cotb",), _cmd_cotb),
    CommandSpec(("qgo",), _cmd_qgo),   # callbacks internos de /queue
    CommandSpec(("qdel",), _cmd_qdel),
    CommandSpec(("fork",), _cmd_fork),
    CommandSpec(("api",), _cmd_api),
    CommandSpec(("ping",), _cmd_ping),
    CommandSpec(("set_webhook", "setwebhook"), _cmd_set_webhook),
    CommandSpec(("delete_webhook", "deletewebhook"), _cmd_delete_webhook),
    CommandSpec(("webhook_info", "webhookinfo"), _cmd_webhook_info),
]
for _spec in SPECS:
    REG.register(*_spec.names)(_spec.fn)

# F37b — comandos lentos: corren en thread (ver handle_command). Derivado
# de las specs: nombre y ALIAS de todo comando slow.
SLOW_ASYNC = True
SLOW_CMDS = {n for s in SPECS if s.slow for n in s.names}
ACK_SLOW = "⏳ En background — el resultado llega en un mensaje aparte."

# F38: comandos lentos EN VUELO — desde F37b corren en thread y eran
# invisibles para /status (Damian: "que status me diga qué se está
# ejecutando ahora mismo"). base → (t0, texto del comando).
INFLIGHT = {}
_INFLIGHT_LOCK = threading.Lock()


def inflight_snapshot():
    """Copia estable de lo que está corriendo: [(base, t0, texto)]."""
    with _INFLIGHT_LOCK:
        return [(b, v[0], v[1]) for b, v in INFLIGHT.items()]


def _run_slow(cfg, text, base):
    with _INFLIGHT_LOCK:
        INFLIGHT[base] = (time.time(), text)
    try:
        r = REG.dispatch(cfg, text)
        if isinstance(r, tuple):
            uhttp.send(cfg, r[0], loud=False, markup=r[1])
        elif r is not None:
            uhttp.send(cfg, r, loud=False)
    except Exception as exc:  # noqa: BLE001 - el thread reporta, no muere
        log("errors", "slow-cmd /%s: %s" % (base, exc))
        uhttp.send(cfg, "⚠️ Error en /%s: %s" % (base, esc(str(exc)[:120])),
                   loud=True)
    finally:
        with _INFLIGHT_LOCK:
            INFLIGHT.pop(base, None)


def handle_command(cfg, text):
    """Comandos del 2_bot (despacho por Command Registry). Devuelve la
    respuesta (HTML, o (texto, markup)) o None si no es comando.

    F37b — directiva de Damian: TODO lo que entra del celu es async, el
    poller NUNCA espera a lo que está en curso. Los comandos que tocan
    protocolo o grace (compact: minutos; ctx/usage/quota: roundtrips al
    runtime; stop: grace de 4s) corren en un thread y su resultado viaja
    como mensaje aparte; acá solo sale el acuse instantáneo. En tests
    SLOW_ASYNC=False (support.fresh): todo sincrónico como siempre."""
    base = ""
    if text.startswith("/"):
        head = text[1:].split()
        base = head[0].split("@")[0].lower() if head else ""
    if SLOW_ASYNC and base in SLOW_CMDS:
        threading.Thread(target=_run_slow, args=(cfg, text, base),
                         daemon=True, name="cmd-%s" % base).start()
        return ACK_SLOW
    return REG.dispatch(cfg, text)
