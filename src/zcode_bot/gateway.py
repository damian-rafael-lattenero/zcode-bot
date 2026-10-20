#!/usr/bin/env python3
"""ZCode → Telegram remote monitor.

Notifica a Telegram todo lo que pasa en ZCode (turno iniciado/terminado,
permisos, preguntas, fallos, sesión trabada, app cerrada) y permite responder
permisos/preguntas desde el móvil. El pedido de permiso es una carrera: si
respondés desde Telegram gana el hook; si no, queda el diálogo de la app.

Setup (una vez):
  1. Crear un bot en @BotFather (ej: glm_lattenero_ctrl_bot) y copiar el token.
  2. zcode-telegram.py set-token <TOKEN>
  3. Mandarle cualquier mensaje al bot nuevo desde Telegram.
  4. zcode-telegram.py register        # aprende tu chat_id y confirma
  5. Reiniciar ZCode para que carguen los hooks.

Uso:
  hook                    (uso interno; lee el payload del hook por stdin)
  set-token <TOKEN>
  register                (espera un mensaje tuyo y bloquea el chat)
  test [texto]
  quota                   (informa y envía la quota z.ai al 2_bot)
  watch-app               (uso interno; loop del LaunchAgent)
  watch-commands          (uso interno; atiende /quota /ping /help del 2_bot)
  --stuck-watch <token> <session_id>   (uso interno)
"""
import asyncio
import os
import sys
import time
import traceback

# Paquete núcleo (F2): state atómico, concurrencia con lock disciplinado,
# factory de protocolo, scanner de logs y repository sqlite.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zcode_bot import auditor as zc_auditor    # noqa: E402  (F30 invariantes)
from zcode_bot import webhook_server as zc_webhook  # noqa: E402

# Utils (Fase 1): config, formatting, validation, http, logging
from zcode_bot.utils.formatting import esc, fmt_tok  # noqa: E402
from zcode_bot.utils.validation import classify, norm  # noqa: E402
from zcode_bot.utils.http import send, send_document, outbox_flush  # noqa: E402
from zcode_bot.utils.logging import log, pwlog  # noqa: E402
from zcode_bot.utils.config import (load_config, save_config,  # noqa: E402
                                    DEFAULTS, configured)

# Paths: SIEMPRE por atributo (config.STATE_DIR) — la fuente única es
# zcode_bot.config y el fixture de tests rebindea ESE módulo (F1e).
# Los cuerpos de load/save/send/log/outbox viven en utils (un solo cuerpo
# vivo; antes el gateway los redefinía y pisaba los imports).
from zcode_bot import config  # noqa: E402
from zcode_bot.commands.callbacks import (  # noqa: E402  R5: vocabulario
    es_respuesta_de_pregunta)
from zcode_bot.hooks import (  # noqa: E402  entrypoints del CLI (F4j)
    marker_path, on_user_prompt, on_stop,
    on_permission_request, on_ask_user_question, on_tool_failure,
    on_session_start, run_hook, cmd_register, cmd_stuck_watch,
    cmd_watch_app)
from zcode_bot.espectador import cmd_espectador  # noqa: E402
from zcode_bot.razonamiento import (cmd_cot_botones,  # noqa: E402  F40/F41
                                    cmd_cot_vivo)  # (F33)
from zcode_bot.commands.dispatch import (  # noqa: E402  despacho (F4i)
    register_bot_menu, handle_free_text, handle_command,
    _task_select_sid, _cmd_cot, _cmd_help, _cmd_nuclear, _cmd_qgo,
    _cmd_qdel)
from zcode_bot.updates import (  # noqa: E402  plumbing Telegram (F4h)
    busy_fresh, current_offset, save_offset, _cb_text,
    react, typing, normalize_update, fetch_updates,
    from_owner, _hq_pending, _reload_now_path, housekeeping)
from zcode_bot.workers import (  # noqa: E402  turnos headless (F4g)
    bot_stop, _user_input_handler, _queue_put, _queue_drain, _queue_resume,
    _active_turn_register, _active_turn_clear, _spawn_worker, cmd_run_turn,
    prompt_worker, _runtime_muerto, _orphans_check, cola_check)
from zcode_bot.reporting import (  # noqa: E402  informes (F4f)
    quota_report, _compact_args, build_trace, _turn_trace,
    stop_list_report)
from zcode_bot.nuclear import (  # noqa: E402  ☢️ Nuclear 2.0 (F4e)
    _nuc_kill_own_runtimes, app_restart_cycle, _nuclear_flag_path,
    _nuclear_heartbeat, _nuclear_active, _nuclear_run, _nuclear_report_path,
    cmd_nuclear_watchdog)
from zcode_bot.live_view import (  # noqa: E402  sidecar del turno (F4d)
    _turn_end_poll, _poll_cadence, _rss_now_mb,
    _live_skip_path, _live_skip_mark, _live_skip_check, _live_skip_clear,
    _headless_text, _cot_chunks_to_send, _cajas_paso_nuevas, HEADLESS_GUARD,
    LIVE_FRAME_TRIP, LIVE_SKIP_TTL, LIVE_BIG_FRAME)
from zcode_bot.messages import (  # noqa: E402  parsers ProtoClient (F4c)
    _extract_assistant_text, _steps_of_messages, _tools_of_messages,
    _tools_con_call_id, _tool_caja, _tool_caja_on,
    _reasoning_of_messages)
from zcode_bot.core import (  # noqa: E402  seam compartido (F4b)
    BotSession, WaitReply,
    _store, _workers, _questions, active_bot_session,
    set_active_bot_session,
    _task_current_ok, _cur_sel, _set_sel,
    running_turns, _queue_peek,
    _stop_file, _stop_request, _stop_requested,
    _stop_consume, _perm_key, _PERM_REPLAY_S)

# R2 (refactor, fase 1): la facade se PODÓ — 268 líneas de padding del
# esqueleto del monolito y 116 re-exports muertos (verificados por nombre
# contra el cuerpo de acá, `zt.X` en la suite y scripts/). La suite
# despacha por handle_command o importa del módulo DUEÑO; quedan solo los
# nombres que main/process_update_sync usan y los que los tests tocan por
# zt.<nombre>. Para agregar un re-export: primero grep en tests/, después
# la línea acá.


def process_update_sync(cfg, msg):
    """El cuerpo por-update del poller (F2c: extraído de cmd_watch_commands
    — movida PURA, semántica idéntica). Es la pieza que comparten el
    driver síncrono, el AsyncPoller (F2d) y el webhook (F3): un update,
    un procesamiento, en orden y serial. Los `continue` del loop eran
    `return` acá."""
    if not from_owner(cfg, msg):
        return
    if msg.get("chat", {}).get("id") != cfg.get("chat_id"):
        # F22: mensaje del owner desde OTRO chat (grupo): responder allá
        # rompería la unidad del chat. FIX 14/10: con rastro — antes se
        # tragaba en silencio y parecía "el bot no responde".
        pwlog("UPDATE de otro chat %s — ignorado (usá el chat privado)" %
              msg.get("chat", {}).get("id"))
        return
    text = (msg.get("text") or "").strip()
    if not text:
        if msg.get("photo") or msg.get("voice") or msg.get("document"):
            # F23: antes los adjuntos caían en silencio
            send(cfg, "📎 Vi tu adjunto — por ahora proceso "
                      "solo texto: pasame la idea en "
                      "palabras.", loud=False)
        return
    cbd = msg.get("_cb_data") or ""
    if es_respuesta_de_pregunta(cbd) and not _hq_pending():
        # botón de una pregunta que ya no está pendiente:
        # NO inyectar el texto a la conversación activa
        send(cfg, "🤔 Ese botón ya venció — no hay pregunta "
                  "pendiente (/task para la lista).", loud=False)
        return
    pwlog("UPDATE:", text[:60])
    react(cfg, msg, "👀")  # F23: recibido
    typing(cfg)  # F23: sin ventana muda
    try:
        if not text.startswith("/"):
            reply = handle_free_text(cfg, text)
        else:
            reply = handle_command(cfg, text)
            if reply is None:
                reply = "🤔 Comando desconocido — /help"
    except Exception as exc:  # noqa: BLE001
        log("errors", "handler: %s" % exc)
        from zcode_bot import audit  # F30: correlación (import tardío: sin ciclo)
        audit.registrar(None, "handler", str(exc)[:150])
        reply = "⚠️ Error interno procesando tu mensaje: %s" % esc(str(exc)[:120])
    if reply is not None:
        if isinstance(reply, tuple):
            send(cfg, reply[0], loud=False, markup=reply[1])
        else:
            send(cfg, reply, loud=False)
        react(cfg, msg, "✅")  # F23: atendido


async def runtime_async():
    """Composition root del proceso poller (F3): sobre UN loop asyncio
    corre el transporte elegido por config — polling (default, probado)
    o webhook (ADR-011, mutuamente excluyentes: un solo lector) — más el
    supervisor (recarga/huérfanos/outbox/menú). El webhook comparte el
    pipeline completo: normalize_update → dispatcher serial →
    process_update_sync."""
    from zcode_bot import poller
    from zcode_bot.dispatcher import UpdateDispatcher

    def _clear_reload():
        try:
            os.remove(_reload_now_path())
        except OSError:
            pass

    deps = dict(
        load_config=load_config,
        configured=configured,
        orphans_check=_orphans_check,
        register_menu=register_bot_menu,
        outbox_flush=outbox_flush,
        auditar=zc_auditor.auditar,  # F30: invariantes (throttle propio)
        queue_drain=cola_check,  # F37a: drain autónomo de colas dormidas
        reload_pending=lambda: os.path.exists(_reload_now_path())
                               and not _nuclear_active(),
        clear_reload=_clear_reload,
        log=log)

    cfg = load_config()
    if cfg.get("transport") == "webhook" and cfg.get("webhook_url"):
        dispatcher = UpdateDispatcher(
            process_update_sync, from_owner,
            on_error=lambda e: log("errors", f"dispatcher: {e}"))
        consumer = asyncio.ensure_future(dispatcher.run())
        runner = await zc_webhook.serve_async(
            cfg, dispatcher, normalize_update,
            lambda: load_config().get("webhook_secret"))
        log("errors", "transporte: webhook (ADR-011) — long-polling OFF")
        try:
            await poller.supervisor_loop(**deps)
        finally:
            await runner.cleanup()
            consumer.cancel()
    else:
        log("errors", "transporte: polling (long-poll 3s)")
        await poller.AsyncPoller(
            fetch=fetch_updates, process=process_update_sync,
            busy_fresh=busy_fresh, current_offset=current_offset,
            save_offset=save_offset, poll_timeout=3,
            on_start=housekeeping, **deps).run_forever()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "hook"
    try:
        if cmd == "hook":
            run_hook()
        elif cmd == "set-token" and len(sys.argv) > 2:
            cfg = load_config()
            cfg["token"] = sys.argv[2].strip()
            save_config(cfg)
            print("Token guardado. Ahora: mandale un mensaje al bot y corré 'register'.")
        elif cmd == "register":
            cmd_register()
        elif cmd == "test":
            cfg = load_config()
            ok = send(cfg, "🧪 Test de avisos de ZCode" + (f": {' '.join(sys.argv[2:])}" if len(sys.argv) > 2 else ""))
            print("Enviado" if ok else "Falló (¿token? ¿register?)")
        elif cmd == "quota":
            cfg = load_config()
            report = quota_report()
            print(report)
            send(cfg, report, loud=False)
        elif cmd == "--stuck-watch":
            cmd_stuck_watch(sys.argv[2], sys.argv[3])
        elif cmd == "--cot-vivo":
            # F41: el CoT del turno de la Mac en vivo — botones que crecen
            cmd_cot_vivo(sys.argv[2], sys.argv[3], sys.argv[4],
                         sys.argv[5] if len(sys.argv) > 5 else os.getcwd())
        elif cmd == "--cot-botones":
            # F40: razonamiento del turno de la Mac → botones CoT n
            cmd_cot_botones(sys.argv[2], sys.argv[3], sys.argv[4],
                            sys.argv[5] if len(sys.argv) > 5 else os.getcwd())
        elif cmd == "--espectador":
            # F33: tail del rollout del turno de la Mac → Telegram
            cmd_espectador(sys.argv[2], sys.argv[3] if len(sys.argv) > 3
                           else os.getcwd())
        elif cmd == "--run-turn":
            cmd_run_turn()  # F16: entrypoint del worker-actor (proceso)
        elif cmd == "--nuclear-watchdog":
            cmd_nuclear_watchdog()  # Nuclear 2.0: cierre verificado (detached)
        elif cmd == "watch-app":
            cmd_watch_app()
        elif cmd == "watch-commands":
            # F3: composition root async — polling o webhook según config
            # (el loop del poller vive en zcode_bot.poller, testeado con
            # fakes en test_poller.py; el webhook en webhook_server.py)
            asyncio.run(runtime_async())
        else:
            print(__doc__)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - jamás romper la sesión por un bug acá
        log("errors", traceback.format_exc())


if __name__ == "__main__":
    main()
