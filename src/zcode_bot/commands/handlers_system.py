"""commands.handlers_system — system, model and webhook handlers (F9).

Moved VERBATIM from commands.dispatch (debt declared in ADR-012, F9
close-out): _cmd_start, _cmd_deploy, _cmd_nuclear, _cmd_ping,
_cmd_model, _cmd_thinking, _cmd_mode, _cmd_fork, _cmd_api and the
three webhook ones. These are the CONTROL commands: they change
configuration, stop things or talk directly to the protocol.
"""
import asyncio
import json
import os
import secrets
import subprocess
import time

_DEPLOY_TIMEOUT_S = 600  # /deploy: ceiling of the bash subprocess (test_cmds_deploy mirrors it)
_HS_TG_TIMEOUT_S = 10     # /handshake: the Telegram probe's ceiling (getMe)

from zcode_bot import config
from zcode_bot import webhook_server as zc_webhook
from zcode_bot import proto as zc_proto
from zcode_bot.core import (active_bot_session, active_workspace_path,
                            set_active_bot_session, _cur_sel, _set_sel,
                            BOT_MODES, bot_mode_of, MODE_ESCRITOR)
from zcode_bot.reporting import _mode_label, MODES
from zcode_bot.models import MODELS, _thinking_levels
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.config import save_config
from zcode_bot.nuclear import _nuclear_run


def _cmd_start(cfg, rest):
    return "🤖 ZCode control bot active.\nCommands: /quota — /ping — /help"


def _cmd_deploy(cfg, rest):
    """/deploy — the full pipeline from the phone (F25): tests → backup →
    copy → instant reload. Two-step confirmation (the 'go' can only be
    produced by the button). Only makes sense if there ARE changes to
    deploy: runs deploy.sh as-is."""
    r = (rest or "").strip().lower()
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    deploy_sh = os.path.join(os.path.dirname(src), "deploy.sh")
    if not os.path.exists(deploy_sh):
        # in production (~/.zcode/hooks) the repo lives in the bot's workspace
        for cand in ("/Users/lattenero/Projects/bot/deploy.sh",):
            if os.path.exists(cand):
                deploy_sh = cand
                break
    if r == "go":
        def run():
            try:
                p = subprocess.run(["bash", deploy_sh], capture_output=True,
                                   text=True, timeout=_DEPLOY_TIMEOUT_S,
                                   cwd=os.path.dirname(deploy_sh))
                tail = (p.stdout or "").strip().splitlines()[-6:]
                ok = p.returncode == 0
                uhttp.send(cfg, ("✅ <b>Deploy OK</b>\n<pre>%s</pre>" % esc("\n".join(tail)[-1500:]))
                     if ok else
                     ("❌ <b>Deploy failed</b> (rc=%d)\n<pre>%s</pre>"
                      % (p.returncode, esc("\n".join(tail)[-1200:]))),
                     loud=not ok)
            except Exception as exc:
                uhttp.send(cfg, "❌ deploy: %s" % esc(str(exc)[:150]), loud=True)
        import threading as _th
        _th.Thread(target=run, daemon=True).start()
        return "🚀 Deploy running (tests included, ~1 min) — I'll let you know."
    if r == "no":
        return "🙅 Cancelled."
    return ("🚀 <b>Deploy</b> — tests → backup → copy → instant poller "
            "reload. Run deploy.sh?",
            {"inline_keyboard": [[
                {"text": "🚀 Deploy", "callback_data": "dep:go"},
                {"text": "🙅 No", "callback_data": "dep:no"}]]})


def _cmd_nuclear(cfg, rest):
    """/nuclear — EMERGENCY: stops EVERYTHING (phone runs + Mac UI tasks)
    and restarts the app and the bot. There is no external input to the
    app's runtime (ADR-006): closing and reopening IS the mechanism. Only
    runs with 'go' — which only the confirmation button produces."""
    r = (rest or "").strip().lower()
    if r == "go":
        import threading as _th
        _th.Thread(target=_nuclear_run, args=(cfg,), daemon=True).start()
        return ("☢️ Nuclear 2.0 starting — the pre-flight tells you what "
                "stops, what is lost and what is kept. The final verdict "
                "arrives verified (post-restart watchdog).")
    if r == "no":
        return "🙅 Cancelled — nothing was touched."
    return ("☢️ <b>Nuclear</b> — emergency:\n"
            "⏹️ stops ALL runs (phone + Mac)\n"
            "🖥️ closes and reopens the ZCode app\n"
            "🔁 restarts the bot\n\nConfirm?",
            {"inline_keyboard": [[
                {"text": "☢️ Confirm", "callback_data": "nuc:go"},
                {"text": "🙅 Cancel", "callback_data": "nuc:no"}]]})


def _cmd_ping(cfg, rest):
    return "🏓 pong"


def _cmd_model(cfg, rest):
    act = active_bot_session()
    if not act:
        return "⚠️ Pick a conversation first (/task n or /task new)."
    sel = _cur_sel(act)
    title = act.get("title") or "?"
    mid = sel.get("modelId", "?")
    th = (sel.get("options") or {}).get("reasoningLevel", "high")
    if not rest:
        row = [{"text": ("● " if mid == m else "○ ") + m,
                 "callback_data": "m:" + m} for m in MODELS]
        return ("🎯 «%s»\n🤖 Model: %s · 🧠 thinking: %s\n"
                "Tap to change (applies on the next turn):"
                % (esc(title), esc(mid), esc(th)),
                {"inline_keyboard": [row]})
    if rest not in MODELS:
        return "⚠️ Models: %s" % " · ".join(MODELS)
    _set_sel(act, model_id=rest)
    return ("🎯 «%s»\n🤖 Model: %s · 🧠 thinking: %s (applies on the next turn)"
            % (esc(title), esc(rest), esc(th)))


def _cmd_thinking(cfg, rest):
    act = active_bot_session()
    if not act:
        return "⚠️ Pick a conversation first (/task n or /task new)."
    sel = _cur_sel(act)
    title = act.get("title") or "?"
    mid = sel.get("modelId", "?")
    th = (sel.get("options") or {}).get("reasoningLevel", "high")
    if not rest:
        row = [{"text": ("● " if th == lv else "○ ") + lv,
                 "callback_data": "th:" + lv} for lv in _thinking_levels(mid)]
        return ("🎯 «%s» · 🤖 %s\n🧠 Thinking: %s\n"
                "Tap to change (applies on the next turn):"
                % (esc(title), esc(mid), esc(th)),
                {"inline_keyboard": [row]})
    levels = _thinking_levels(mid)
    lv = rest.lower()
    if lv not in levels:
        return "⚠️ Levels: %s" % " · ".join(levels)
    _set_sel(act, thinking=lv)
    return ("🎯 «%s»\n🧠 Thinking: %s · model %s (applies on the next turn)"
            % (esc(title), esc(lv), esc(mid)))


def _cmd_verbosity(cfg, rest):
    """/verbosity — F61: ONE level for how much a turn streams to the
    phone (Damian, 24/10): summary / normal / verbose — the same
    vocabulary as the app's fork (messageStreamVerbosity). It is a PRESET
    over the existing knobs, persisted in the config (user-scope like the
    app's: it depends on where YOU are, not on the conversation). Warm
    workers refresh it on pickup — it applies on the NEXT turn."""
    from zcode_bot import verbosity as vb
    cur = vb.level_of(cfg)
    r = (rest or "").strip().lower()
    if not r:
        row = [{"text": ("● " if cur == lv else "○ ") + lv,
                "callback_data": "vb:" + lv} for lv in vb.LEVELS]
        return ("🎚 Verbosity: <b>%s</b>\n%s\n\nTap to change (applies on "
                "the next turn):"
                % (cur,
                   "\n".join("• <b>%s</b> — %s" % (lv, vb.DESCRIBE[lv])
                             for lv in vb.LEVELS)),
                {"inline_keyboard": [row]})
    if r not in vb.LEVELS:
        return "⚠️ Levels: %s" % " · ".join(vb.LEVELS)
    vb.apply_level(cfg, r)
    save_config(cfg)
    return ("🎚 Verbosity: <b>%s</b> — %s\n(applies on the next turn)"
            % (r, vb.DESCRIBE[r]))


def _cmd_mode(cfg, rest):
    """/mode — just two options (Damian's request 2026-10-13):
    Plan mode (read/plan, nothing is touched) and Full access (yolo: full
    disk, no brakes). The value travels to the protocol all the same
    (setMode)."""
    act = active_bot_session()
    if not act:
        return "⚠️ Pick a conversation first (/task n)."
    modes = tuple(m for m, _ in MODES)
    current = act.get("mode") or "yolo"
    if not rest:
        row = [{"text": ("● " if current == m else "○ ") + label,
                 "callback_data": "mo:" + m} for m, label in MODES]
        return ("🎯 «%s»\n🎛 Mode: %s\nTap to change (applies on the next "
                "turn):" % (esc(act.get("title") or "?"),
                            esc(_mode_label(current))),
                {"inline_keyboard": [[b] for b in row]})
    if rest not in modes:
        return "⚠️ Modes: " + " · ".join(modes)
    set_active_bot_session(dict(act, mode=rest))
    return ("🎛 Mode of «%s»: %s (applies on the next turn)"
            % (esc(act.get("title") or "?"), esc(_mode_label(rest))))


def _cmd_bot_mode(cfg, rest):
    """/bot_mode — the two ways of using this chat (Damian, 27/10):
    lector (DEFAULT): the chat only WATCHES — free text is refused with
    «modo lector»; /task moves what streams, nothing is written.
    escritor: free text runs in the active conversation (headless);
    while the Mac is mid-turn the text QUEUES (with the interrupt
    button) — reading and writing never collide. The reading stream
    itself is mode-independent: /task moves it in both."""
    act = active_bot_session()
    cur = bot_mode_of(act)
    r = (rest or "").strip().lower()
    if not r:
        row = [{"text": ("● " if cur == m else "○ ") + m,
                "callback_data": "bm:" + m} for m in BOT_MODES]
        return ("🎛 Bot mode: <b>%s</b>\n"
                "• <b>lector</b> — the chat only watches; text is "
                "refused\n"
                "• <b>escritor</b> — your text runs in the active "
                "conversation\n\nTap to change:" % cur,
                {"inline_keyboard": [row]})
    if r not in BOT_MODES:
        return "⚠️ Modes: " + " · ".join(BOT_MODES)
    if act:
        set_active_bot_session(dict(act, bot_mode=r))
    else:
        set_active_bot_session({"sid": "", "title": "", "ws": "",
                                "bot_mode": r})
    if r == MODE_ESCRITOR and act and act.get("sid"):
        return ("🎛 Bot mode: <b>escritor</b> — your text runs in «%s» "
                "(queued + interruptible while the Mac is mid-turn)"
                % esc(act.get("title") or "?"))
    return "🎛 Bot mode: <b>%s</b>" % r


def _cmd_handshake(cfg, rest):
    """/handshake — the pipe's health in ONE message, and it DOES the
    handshake when it is missing (Damian, 27/10: «tambien produce el  # noqa: spanish — Damian's request, quoted
    efecto de hacer handshake sino esta hecho ya»): Telegram latency,  # noqa: spanish — Damian's request, quoted (continuation)
    the reading focus (turn running? watcher alive? sidecar fresh?),
    today's network errors — and when the focus runs but its watcher
    is dead, the re-attach runs RIGHT HERE (the watchers respawn
    mid-turn; the boxes the chat never saw land at the first sync)."""
    from zcode_bot import live_io
    lines = ["🤝 <b>handshake</b>"]
    t0 = time.time()
    try:
        uhttp.tg_api(cfg, "getMe", timeout=_HS_TG_TIMEOUT_S)
        lines.append("Telegram: 🟢 %.1fs" % (time.time() - t0))
    except Exception as exc:  # noqa: BLE001
        lines.append("Telegram: 🔴 %s" % esc(str(exc)[:80]))
    act = active_bot_session()
    focus = (act.get("focus_sid") or act.get("sid") or "") if act else ""
    lines.append("Mode: %s · focus: %s" % (
        esc(bot_mode_of(act)),
        esc((act.get("title") or focus[-8:]) if act else "—")))
    if focus:
        turn = os.path.exists(config.marker_path(focus))
        lines.append("Mac turn: %s" % ("🟢 running" if turn else "— none"))
        from zcode_bot import reasoning
        up = reasoning.watcher_alive(focus)
        try:
            age = time.time() - os.path.getmtime(reasoning.pid_path(focus))
        except OSError:
            age = -1.0
        lines.append("Watcher cot-live: %s" % (
            "🟢 (beat %.0fs)" % age if up else "🔴 dead"))
        sc = live_io.sidecar_path(focus)
        if os.path.exists(sc):
            lines.append("Sidecar live-io: 🟢 %.0fs old"
                         % (time.time() - os.path.getmtime(sc)))
        elif turn:
            lines.append("Sidecar live-io: 🔴 none with a turn running — "
                         "the runtime does not flush the live CoT "
                         "(desktop app: ⌘Q and reopen)")
        else:
            lines.append("Sidecar live-io: — none (nothing thinking)")
        if not up and turn:
            # F77 — the command DOES the handshake, it does not only
            # report it (Damian: «tambien produce el efecto de hacer  # noqa: spanish — Damian's request, quoted
            # handshake sino esta hecho ya»): the re-attach respawns  # noqa: spanish — Damian's request, quoted (continuation)
            # the watchers mid-turn (cot-live in attach mode — the
            # boxes the chat never saw land at its first sync)
            from zcode_bot import attach as zc_attach
            try:
                res = zc_attach.mac_attach(
                    cfg, focus, (act.get("title") if act else "")
                    or focus[-8:], (act.get("ws") if act else "") or "")
                lines.append("🔌 Re-attached: %s" % esc(str(res)[:140]))
            except Exception as exc:  # noqa: BLE001
                lines.append("🔴 Re-attach failed: %s"
                             % esc(str(exc)[:100]))
    nerr = 0
    try:
        today = "[%s" % time.strftime("%Y-%m-%d")
        with open(os.path.join(config.LOG_DIR, "errors.log")) as f:
            nerr = sum(1 for ln in f
                       if ln.startswith(today) and "failed" in ln)
    except OSError:
        pass
    lines.append("Network today: %s" % (nerr or "clean"))
    return "\n".join(lines)


def _cmd_fork(cfg, rest):
    act = active_bot_session()
    if not act or not act.get("sid"):
        return "⚠️ Pick a conversation first (/task n)."
    try:
        c = zc_proto.make(cwd=act["ws"])
    except OSError as e:
        return "❌ " + esc(str(e)[:100])
    try:
        r = c.request("session/fork", {"sessionId": act["sid"]}, timeout=60)
        if e := zc_proto.proto_err(r, "fork"):
            return e
        res = r.get("result") or {}
        nsid = res.get("sessionId") or (res.get("session") or {}).get("sessionId") or res.get("id")
        if not nsid:
            return "❌ fork with no sessionId: " + esc(json.dumps(res)[:120])
        set_active_bot_session(dict(act, sid=nsid, focus_sid=nsid,
                                    title=(act.get("title") or "") + " (fork)"))
        return "🍴 Fork created and active."
    finally:
        c.close()


def _cmd_api(cfg, rest):
    if not rest:
        return "⚠️ /api &lt;method&gt; [params json]"
    parts = rest.split(" ", 1)
    method = parts[0]
    params = {}
    if len(parts) == 2 and parts[1].strip():
        try:
            params = json.loads(parts[1])
        except ValueError as e:
            return "⚠️ Invalid JSON: %s" % esc(str(e)[:100])
    try:
        c = zc_proto.make(cwd=active_workspace_path())
    except OSError as e:
        return "❌ " + esc(str(e)[:100])
    try:
        r = c.request(method, params, timeout=60)
        return "🔌 %s\n%s" % (esc(method), esc(json.dumps(r, ensure_ascii=False)[:2800]))
    finally:
        c.close()


def _cmd_set_webhook(cfg, rest):
    """/set_webhook <URL> [secret] — sets up a Telegram webhook.
    Without a secret one is GENERATED (token_urlsafe): the receiver is
    fail-closed (without a secret, 401 to everything) — the URL alone
    never suffices."""
    if not rest:
        return "⚠️ Usage: /set_webhook <URL> [secret_token]"
    parts = rest.split(" ", 1)
    url = parts[0].strip()
    secret = parts[1].strip() if len(parts) > 1 else secrets.token_urlsafe(32)

    if not url.startswith("https://"):
        return "⚠️ The URL must be HTTPS"

    ok = asyncio.run(zc_webhook.set_webhook(cfg, url, secret))
    if ok:
        cfg["webhook_url"] = url
        cfg["webhook_secret"] = secret
        cfg["transport"] = "webhook"  # ADR-011: mutually exclusive transports
        save_config(cfg)
        return (f"✅ Webhook set up: {url}\n"
                f"🔐 secret saved in config ({secret[:6]}…). "
                f"The poller picks up the webhook on its next start "
                f"(reload with /deploy go).")
    else:
        return "❌ Webhook setup failed"


def _cmd_delete_webhook(cfg, rest):
    """/delete_webhook — removes the webhook and goes back to long-polling."""
    ok = asyncio.run(zc_webhook.delete_webhook(cfg))
    if ok:
        cfg.pop("webhook_url", None)
        cfg.pop("webhook_secret", None)
        cfg["transport"] = "polling"
        save_config(cfg)
        return "✅ Webhook deleted (back to long-polling)"
    else:
        return "❌ Webhook deletion failed"


def _cmd_webhook_info(cfg, rest):
    """/webhook_info — shows info about the current webhook."""
    info = asyncio.run(zc_webhook.get_webhook_info(cfg))
    return json.dumps(info, indent=2, ensure_ascii=False)
