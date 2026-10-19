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
from zcode_bot.commands.handlers_tareas import (  # noqa: F401
    task_select, _task_select_sid, handle_free_text, _cmd_retry, _cmd_stop,
    _cmd_task, _cmd_all_tasks, _cmd_queue, _queue_mutate, _cmd_qgo,
    _cmd_qdel, _cmd_run)
from zcode_bot.commands.handlers_info import (  # noqa: F401
    register_bot_menu, _cmd_quota, _cmd_usage, _cmd_ctx, _cmd_compact,
    _cmd_all_compact, _cmd_status, _cmd_last, _cmd_cot, _cmd_help)
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

# F37b — comandos lentos: corren en thread (ver handle_command). El resto
# responde al instante por sí solo (estado del bot / thread propio).
SLOW_ASYNC = True
SLOW_CMDS = {"compact", "all_compact", "allcompact", "ctx", "contexto",
             "usage", "uso", "quota", "stop", "all_stop", "allstop"}
ACK_SLOW = "⏳ En background — el resultado llega en un mensaje aparte."


def _run_slow(cfg, text, base):
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


for _names, _fn in [
    (("start",), _cmd_start),
    (("quota",), _cmd_quota),
    (("usage", "uso"), _cmd_usage),
    (("ctx", "contexto"), _cmd_ctx),
    (("compact",), _cmd_compact),
    (("all_compact", "allcompact"), _cmd_all_compact),
    (("stop", "all_stop", "allstop"), _cmd_stop),
    (("nuclear", "nuke"), _cmd_nuclear),
    (("deploy",), _cmd_deploy),
    (("task",), _cmd_task),
    (("all_tasks", "alltasks", "tasks"), _cmd_all_tasks),
    (("status",), _cmd_status),
    (("last",), _cmd_last),
    (("cot", "trace"), _cmd_cot),
    (("run",), _cmd_run),
    (("model",), _cmd_model),
    (("thinking",), _cmd_thinking),
    (("mode",), _cmd_mode),
    (("retry",), _cmd_retry),
    (("queue", "cola"), _cmd_queue),
    (("qgo",), _cmd_qgo),      # callbacks internos de /queue (ocultos)
    (("qdel",), _cmd_qdel),
    (("fork",), _cmd_fork),
    (("api",), _cmd_api),
    (("ping",), _cmd_ping),
    (("help",), _cmd_help),
    # Webhook commands
    (("set_webhook", "setwebhook"), _cmd_set_webhook),
    (("delete_webhook", "deletewebhook"), _cmd_delete_webhook),
    (("webhook_info", "webhookinfo"), _cmd_webhook_info),
]:
    REG.register(*_names)(_fn)


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
