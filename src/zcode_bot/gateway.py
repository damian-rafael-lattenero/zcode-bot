#!/usr/bin/env python3
"""ZCode → Telegram remote monitor.

Notifies Telegram of everything happening in ZCode (turn started/ended,
permissions, questions, failures, stuck session, app closed) and lets you
answer permissions/questions from the phone. The permission request is a
race: if you answer from Telegram the hook wins; otherwise the app's
dialog stays up.

Setup (once):
  1. Create a bot with @BotFather (e.g. glm_lattenero_ctrl_bot) and copy the token.
  2. zcode-telegram.py set-token <TOKEN>
  3. Send any message to the new bot from Telegram.
  4. zcode-telegram.py register        # learns your chat_id and confirms
  5. Restart ZCode so the hooks load.

Usage:
  hook                    (internal use; reads the hook payload from stdin)
  set-token <TOKEN>
  register                (waits for one of your messages and blocks the chat)
  test [text]
  quota                   (reports and sends the z.ai quota to 2_bot)
  watch-app               (internal use; LaunchAgent loop)
  watch-commands          (internal use; serves /quota /ping /help for 2_bot)
  --stuck-watch <token> <session_id>   (internal use)
"""
import asyncio
import os
import sys
import time
import traceback

# Core package (F2): atomic state, disciplined-lock concurrency,
# protocol factory, log scanner and sqlite repository.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zcode_bot import auditor as zc_auditor    # noqa: E402  (F30 invariants)
from zcode_bot import webhook_server as zc_webhook  # noqa: E402

# Utils (Phase 1): config, formatting, validation, http, logging
from zcode_bot.utils.formatting import esc, fmt_tok  # noqa: E402
from zcode_bot.utils.validation import classify, norm  # noqa: E402
from zcode_bot.utils.http import send, send_document, outbox_flush  # noqa: E402
from zcode_bot.utils.logging import log, pwlog  # noqa: E402
from zcode_bot.utils.config import (load_config, save_config,  # noqa: E402
                                    DEFAULTS, configured)

# Paths: ALWAYS by attribute (config.STATE_DIR) — the single source is
# zcode_bot.config and the test fixture rebinds THAT module (F1e).
# The load/save/send/log/outbox bodies live in utils (one living body;
# before, the gateway redefined them and shadowed the imports).
from zcode_bot import config  # noqa: E402
from zcode_bot.commands.callbacks import (  # noqa: E402  R5: vocabulario
    is_question_reply)
from zcode_bot.hooks import (  # noqa: E402  CLI entrypoints (F4j)
    marker_path, on_user_prompt, on_stop,
    on_permission_request, on_ask_user_question, on_tool_failure,
    on_session_start, run_hook, cmd_register, cmd_stuck_watch,
    cmd_watch_app)
from zcode_bot.espectador import cmd_spectator  # noqa: E402
from zcode_bot.razonamiento import (cmd_cot_buttons,  # noqa: E402  F40/F41
                                    cmd_cot_live)  # (F33)
from zcode_bot.commands.dispatch import (  # noqa: E402  dispatch (F4i)
    register_bot_menu, handle_free_text, handle_command, known_command,
    _task_select_sid, _cmd_cot, _cmd_help, _cmd_nuclear, _cmd_qgo,
    _cmd_qdel)
from zcode_bot.updates import (  # noqa: E402  Telegram plumbing (F4h)
    busy_fresh, current_offset, save_offset, _cb_text,
    react, typing, normalize_update, fetch_updates,
    from_owner, _hq_pending, _reload_now_path, housekeeping)
from zcode_bot.workers import (  # noqa: E402  headless turns (F4g)
    bot_stop, _user_input_handler, _queue_put, _queue_drain, _queue_resume,
    _active_turn_register, _active_turn_clear, _spawn_worker, cmd_run_turn,
    prompt_worker, _runtime_dead, _orphans_check, queue_check)
from zcode_bot.reporting import (  # noqa: E402  reports (F4f)
    quota_report, _compact_args, build_trace, _turn_trace,
    stop_list_report)
from zcode_bot.nuclear import (  # noqa: E402  ☢️ Nuclear 2.0 (F4e)
    _nuc_kill_own_runtimes, app_restart_cycle, _nuclear_flag_path,
    _nuclear_heartbeat, _nuclear_active, _nuclear_run, _nuclear_report_path,
    cmd_nuclear_watchdog)
from zcode_bot.live_view import (  # noqa: E402  turn sidecar (F4d)
    _turn_end_poll, _poll_cadence, _rss_now_mb,
    _live_skip_path, _live_skip_mark, _live_skip_check, _live_skip_clear,
    _headless_text, _cot_chunks_to_send, _cajas_paso_nuevas, HEADLESS_GUARD,
    LIVE_FRAME_TRIP, LIVE_SKIP_TTL, LIVE_BIG_FRAME)
from zcode_bot.messages import (  # noqa: E402  ProtoClient parsers (F4c)
    _extract_assistant_text, _steps_of_messages, _tools_of_messages,
    _tools_with_call_id, _tool_box, _tool_box_on,
    _reasoning_of_messages)
from zcode_bot.core import (  # noqa: E402  shared seam (F4b)
    BotSession, WaitReply,
    _store, _workers, _questions, active_bot_session,
    set_active_bot_session,
    _task_current_ok, _cur_sel, _set_sel,
    running_turns, _queue_peek,
    _stop_file, _stop_request, _stop_requested,
    _stop_consume, _perm_key, _PERM_REPLAY_S)

# R2 (refactor, phase 1): the facade was PRUNED — 268 lines of monolith
# skeleton padding and 116 dead re-exports (verified by name against this
# body, `zt.X` in the suite and scripts/). The suite dispatches via
# handle_command or imports from the OWNING module; only the names that
# main/process_update_sync use and the ones the tests touch via
# zt.<name> remain. To add a re-export: grep tests/ first, then the
# line here.


def process_update_sync(cfg, msg):
    """The poller's per-update body (F2c: extracted from cmd_watch_commands
    — a PURE move, identical semantics). This is the piece shared by the
    sync driver, the AsyncPoller (F2d) and the webhook (F3): one update,
    one processing, in order and serial. The loop's `continue`s were
    `return`s here."""
    if not from_owner(cfg, msg):
        return
    if msg.get("chat", {}).get("id") != cfg.get("chat_id"):
        # F22: owner message from ANOTHER chat (group): replying there
        # would break chat unity. FIX 14/10: with a trace — before it was
        # swallowed silently and looked like "the bot doesn't answer".
        pwlog("UPDATE from another chat %s — ignored (use the private chat)" %
              msg.get("chat", {}).get("id"))
        return
    text = (msg.get("text") or "").strip()
    if not text:
        if msg.get("photo") or msg.get("voice") or msg.get("document"):
            # F23: attachments used to fall through silently
            send(cfg, "📎 I saw your attachment — for now I only process "
                      "text: give me the idea in words.", loud=False)
        return
    cbd = msg.get("_cb_data") or ""
    if is_question_reply(cbd) and not _hq_pending():
        # button of a question no longer pending:
        # do NOT inject the text into the active conversation
        send(cfg, "🤔 That button has expired — there is no pending "
                  "question (/task for the list).", loud=False)
        return
    pwlog("UPDATE:", text[:60])
    react(cfg, msg, "👀")  # F23: received
    typing(cfg)  # F23: no silent window
    try:
        if not text.startswith("/"):
            reply = handle_free_text(cfg, text)
        else:
            reply = handle_command(cfg, text)
            if reply is None and not known_command(text):
                # F48a: None also means «the handler already answered on
                # its own» (in-place edit, long send) — the notice is
                # only for what does NOT exist
                reply = "🤔 Unknown command — /help"
    except Exception as exc:  # noqa: BLE001
        log("errors", "handler: %s" % exc)
        from zcode_bot import audit  # F30: correlation (late import: no cycle)
        audit.registrar(None, "handler", str(exc)[:150])
        reply = "⚠️ Internal error processing your message: %s" % esc(str(exc)[:120])
    if reply is not None:
        if isinstance(reply, tuple):
            send(cfg, reply[0], loud=False, markup=reply[1])
        else:
            send(cfg, reply, loud=False)
        react(cfg, msg, "✅")  # F23: handled


async def runtime_async():
    """Composition root of the poller process (F3): over ONE asyncio loop
    it runs the transport chosen by config — polling (default,
    battle-tested) or webhook (ADR-011, mutually exclusive: a single
    reader) — plus the supervisor (reload/orphans/outbox/menu). The
    webhook shares the full pipeline: normalize_update → serial
    dispatcher → process_update_sync."""
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
        auditar=zc_auditor.run_audit,  # F30: invariants (own throttle)
        queue_drain=queue_check,  # F37a: autonomous drain of dormant queues
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
        log("errors", "transport: webhook (ADR-011) — long-polling OFF")
        try:
            await poller.supervisor_loop(**deps)
        finally:
            await runner.cleanup()
            consumer.cancel()
    else:
        log("errors", "transport: polling (long-poll 3s)")
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
            print("Token saved. Now: send the bot a message and run 'register'.")
        elif cmd == "register":
            cmd_register()
        elif cmd == "test":
            cfg = load_config()
            ok = send(cfg, "🧪 ZCode notifications test" + (f": {' '.join(sys.argv[2:])}" if len(sys.argv) > 2 else ""))
            print("Sent" if ok else "Failed (token? register?)")
        elif cmd == "quota":
            cfg = load_config()
            report = quota_report()
            print(report)
            send(cfg, report, loud=False)
        elif cmd == "--stuck-watch":
            cmd_stuck_watch(sys.argv[2], sys.argv[3])
        elif cmd == "--cot-vivo":
            # F41: the Mac turn's CoT live — buttons that grow
            cmd_cot_live(sys.argv[2], sys.argv[3], sys.argv[4],
                         sys.argv[5] if len(sys.argv) > 5 else os.getcwd())
        elif cmd == "--cot-botones":
            # F40: Mac turn reasoning → CoT n buttons
            cmd_cot_buttons(sys.argv[2], sys.argv[3], sys.argv[4],
                            sys.argv[5] if len(sys.argv) > 5 else os.getcwd())
        elif cmd == "--espectador":
            # F33: tail of the Mac turn's rollout → Telegram
            cmd_spectator(sys.argv[2], sys.argv[3] if len(sys.argv) > 3
                          else os.getcwd())
        elif cmd == "--run-turn":
            cmd_run_turn()  # F16: worker-actor entrypoint (process)
        elif cmd == "--nuclear-watchdog":
            cmd_nuclear_watchdog()  # Nuclear 2.0: verified shutdown (detached)
        elif cmd == "watch-app":
            cmd_watch_app()
        elif cmd == "watch-commands":
            # F3: async composition root — polling or webhook per config
            # (the poller loop lives in zcode_bot.poller, tested with
            # fakes in test_poller.py; the webhook in webhook_server.py)
            asyncio.run(runtime_async())
        else:
            print(__doc__)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - never break the session over a bug here
        log("errors", traceback.format_exc())


if __name__ == "__main__":
    main()
