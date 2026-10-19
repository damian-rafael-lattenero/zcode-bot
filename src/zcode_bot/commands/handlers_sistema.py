"""commands.handlers_sistema — handlers de sistema, modelo y webhook (F9).

Movido TEXTUAL de commands.dispatch (deuda declarada en ADR-012, cierre
F9): _cmd_start, _cmd_deploy, _cmd_nuclear, _cmd_ping, _cmd_model,
_cmd_thinking, _cmd_mode, _cmd_fork, _cmd_api y los tres de webhook.
Son los comandos de CONTROL: cambian configuración, frenan cosas o
hablan directo con el protocolo.
"""
import asyncio
import json
import os
import secrets
import subprocess

_DEPLOY_TIMEOUT_S = 600  # /deploy: techo del subprocess bash (test_cmds_deploy lo espeja)

from zcode_bot import webhook_server as zc_webhook
from zcode_bot import proto as zc_proto
from zcode_bot.core import (active_bot_session, active_workspace_path,
                            set_active_bot_session, _cur_sel, _set_sel)
from zcode_bot.reporting import _modo_label, MODOS
from zcode_bot.models import MODELOS, _thinking_levels
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.config import save_config
from zcode_bot.nuclear import _nuclear_run


def _cmd_start(cfg, rest):
    return "🤖 Bot de control de ZCode activo.\nComandos: /quota — /ping — /help"


def _cmd_deploy(cfg, rest):
    """/deploy — el pipeline completo desde el celular (F25): tests →
    backup → copia → recarga instantánea. Confirmación en dos pasos
    (el 'go' solo lo produce el botón). Solo tiene sentido si HAY cambios
    que deployar: corre deploy.sh tal cual."""
    r = (rest or "").strip().lower()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)))
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
                                   text=True, timeout=_DEPLOY_TIMEOUT_S,
                                   cwd=os.path.dirname(deploy_sh))
                tail = (p.stdout or "").strip().splitlines()[-6:]
                ok = p.returncode == 0
                uhttp.send(cfg, ("✅ <b>Deploy OK</b>\n<pre>%s</pre>" % esc("\n".join(tail)[-1500:]))
                     if ok else
                     ("❌ <b>Deploy falló</b> (rc=%d)\n<pre>%s</pre>"
                      % (p.returncode, esc("\n".join(tail)[-1200:]))),
                     loud=not ok)
            except Exception as exc:
                uhttp.send(cfg, "❌ deploy: %s" % esc(str(exc)[:150]), loud=True)
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


def _cmd_nuclear(cfg, rest):
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


def _cmd_ping(cfg, rest):
    return "🏓 pong"


def _cmd_model(cfg, rest):
    act = active_bot_session()
    if not act:
        return "⚠️ Elegí conversación primero (/task n o /task new)."
    sel = _cur_sel(act)
    titulo = act.get("title") or "?"
    mid = sel.get("modelId", "?")
    th = (sel.get("options") or {}).get("reasoningLevel", "high")
    if not rest:
        fila = [{"text": ("● " if mid == m else "○ ") + m,
                 "callback_data": "m:" + m} for m in MODELOS]
        return ("🎯 «%s»\n🤖 Modelo: %s · 🧠 thinking: %s\n"
                "Tocá para cambiar (aplica en el próximo turno):"
                % (esc(titulo), esc(mid), esc(th)),
                {"inline_keyboard": [fila]})
    if rest not in MODELOS:
        return "⚠️ Modelos: %s" % " · ".join(MODELOS)
    _set_sel(act, model_id=rest)
    return ("🎯 «%s»\n🤖 Modelo: %s · 🧠 thinking: %s (aplica en el próximo turno)"
            % (esc(titulo), esc(rest), esc(th)))


def _cmd_thinking(cfg, rest):
    act = active_bot_session()
    if not act:
        return "⚠️ Elegí conversación primero (/task n o /task new)."
    sel = _cur_sel(act)
    titulo = act.get("title") or "?"
    mid = sel.get("modelId", "?")
    th = (sel.get("options") or {}).get("reasoningLevel", "high")
    if not rest:
        fila = [{"text": ("● " if th == lv else "○ ") + lv,
                 "callback_data": "th:" + lv} for lv in _thinking_levels(mid)]
        return ("🎯 «%s» · 🤖 %s\n🧠 Thinking: %s\n"
                "Tocá para cambiar (aplica en el próximo turno):"
                % (esc(titulo), esc(mid), esc(th)),
                {"inline_keyboard": [fila]})
    niveles = _thinking_levels(mid)
    lv = rest.lower()
    if lv not in niveles:
        return "⚠️ Niveles: %s" % " · ".join(niveles)
    _set_sel(act, thinking=lv)
    return ("🎯 «%s»\n🧠 Thinking: %s · modelo %s (aplica en el próximo turno)"
            % (esc(titulo), esc(lv), esc(mid)))


def _cmd_mode(cfg, rest):
    """/mode — dos opciones nada más (pedido de Damian 2026-10-13):
    Plan mode (lectura/plan, nada se toca) y Full access (yolo: disco
    completo sin frenos). El valor viaja igual al protocolo (setMode)."""
    act = active_bot_session()
    if not act:
        return "⚠️ Elegí conversación primero (/task n)."
    modos = tuple(m for m, _ in MODOS)
    actual = act.get("mode") or "yolo"
    if not rest:
        fila = [{"text": ("● " if actual == m else "○ ") + label,
                 "callback_data": "mo:" + m} for m, label in MODOS]
        return ("🎯 «%s»\n🎛 Modo: %s\nTocá para cambiar (aplica en el "
                "próximo turno):" % (esc(act.get("title") or "?"),
                                     esc(_modo_label(actual))),
                {"inline_keyboard": [[b] for b in fila]})
    if rest not in modos:
        return "⚠️ Modos: " + " · ".join(modos)
    set_active_bot_session(dict(act, mode=rest))
    return ("🎛 Modo de «%s»: %s (aplica en el próximo turno)"
            % (esc(act.get("title") or "?"), esc(_modo_label(rest))))


def _cmd_fork(cfg, rest):
    act = active_bot_session()
    if not act or not act.get("sid"):
        return "⚠️ Elegí conversación primero (/task n)."
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
            return "❌ fork sin sessionId: " + esc(json.dumps(res)[:120])
        set_active_bot_session(dict(act, sid=nsid,
                                    title=(act.get("title") or "") + " (fork)"))
        return "🍴 Fork creado y activo."
    finally:
        c.close()


def _cmd_api(cfg, rest):
    if not rest:
        return "⚠️ /api &lt;método&gt; [json de params]"
    partes = rest.split(" ", 1)
    metodo = partes[0]
    params = {}
    if len(partes) == 2 and partes[1].strip():
        try:
            params = json.loads(partes[1])
        except ValueError as e:
            return "⚠️ JSON inválido: %s" % esc(str(e)[:100])
    try:
        c = zc_proto.make(cwd=active_workspace_path())
    except OSError as e:
        return "❌ " + esc(str(e)[:100])
    try:
        r = c.request(metodo, params, timeout=60)
        return "🔌 %s\n%s" % (esc(metodo), esc(json.dumps(r, ensure_ascii=False)[:2800]))
    finally:
        c.close()


def _cmd_set_webhook(cfg, rest):
    """/set_webhook <URL> [secret] — configura webhook de Telegram.
    Sin secret se GENERA uno (token_urlsafe): el receiver es fail-closed
    (sin secret, 401 a todo) — la URL sola nunca basta."""
    if not rest:
        return "⚠️ Uso: /set_webhook <URL> [secret_token]"
    partes = rest.split(" ", 1)
    url = partes[0].strip()
    secret = partes[1].strip() if len(partes) > 1 else secrets.token_urlsafe(32)

    if not url.startswith("https://"):
        return "⚠️ La URL debe ser HTTPS"

    ok = asyncio.run(zc_webhook.set_webhook(cfg, url, secret))
    if ok:
        cfg["webhook_url"] = url
        cfg["webhook_secret"] = secret
        cfg["transport"] = "webhook"  # ADR-011: transportes excluyentes
        save_config(cfg)
        return (f"✅ Webhook configurado: {url}\n"
                f"🔐 secret guardado en config ({secret[:6]}…). "
                f"El poller toma el webhook en su próximo arranque "
                f"(recarga con /deploy go).")
    else:
        return "❌ Falló configuración de webhook"


def _cmd_delete_webhook(cfg, rest):
    """/delete_webhook — elimina webhook y vuelve a long-polling."""
    ok = asyncio.run(zc_webhook.delete_webhook(cfg))
    if ok:
        cfg.pop("webhook_url", None)
        cfg.pop("webhook_secret", None)
        cfg["transport"] = "polling"
        save_config(cfg)
        return "✅ Webhook eliminado (vuelve a long-polling)"
    else:
        return "❌ Falló eliminación de webhook"


def _cmd_webhook_info(cfg, rest):
    """/webhook_info — muestra info del webhook actual."""
    info = asyncio.run(zc_webhook.get_webhook_info(cfg))
    return json.dumps(info, indent=2, ensure_ascii=False)
