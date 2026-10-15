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
import traceback
import subprocess

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send
from zcode_bot.utils.logging import log, pwlog

from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, running_turns
from zcode_bot.state.manager import (_active_turn_register, _active_turn_clear,
    _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check,
    _q_gemela, _user_input_handler, _stop_request, _stop_consume,
    _save_last_prompt, _model_snapshot, _restore_model, _proto_ready,
    _create_session, _headless_text, _fetch_messages, _last_id_of,
    _turn_end_poll, _live_send, _live_edit, _live_view, _orphans_handoff,
    _queue_drain, _rss_mb, _rss_now_mb)
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles
from zcode_bot.workers.nuclear import _nuclear_active

from zcode_bot.utils.config import BASE, STATE_DIR, LOG_DIR, MODEL_SEL


def _stop_file(sid):
    d = os.path.join(STATE_DIR, "bot-stop")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sid or "ses")
    return os.path.join(d, safe)


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


def _stop_requested(sid):
    return bool(sid) and os.path.exists(_stop_file(sid))


def _stop_consume(sid):
    try:
        os.remove(_stop_file(sid))
    except OSError:
        pass


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


def prompt_worker(cfg, act, text):
    """Corre un prompt headless en la conversación activa y empuja la
    respuesta a Telegram. Vive en un PROCESO (F16: actor)."""
    sid, ws, titulo = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    key = sid or "nueva"
    abandonado = [False]  # F21: fin=None → el runtime se hereda al supervisor
    if sid and WORKERS.get(sid) is not None:
        # ya hay un worker para la sesión (carrera poller/queue_drain):
        # encolar sin avisar — el que corre hace drain al terminar
        _queue_put(sid, act, text)
        return
    if sid and sid in running_turns():
        # F17: la task está ocupada (corrida de la Mac, sin worker nuestro)
        # → cola DORMIDA, sin spawnnear runtime (el relay en loop gastaba
        # ~500MB por ciclo). La despierta quien termina: on_stop de la Mac
        # o el drain del worker.
        pwlog("busy -> encolar (task ocupada, sin runtime)")
        _queue_put(sid, act, text)
        send(cfg, "⏳ La Mac está ocupada con «%s» — tu mensaje quedó en "
                  "la cola (%d en espera). Corre al terminar la corrida "
                  "(o /stop para limpiarla)."
                  % (esc(titulo), len(_queue_peek(sid))))
        return
    WORKERS.register(key, {"t0": time.time(), "prompt": text[:80],
                           "ws": ws, "titulo": titulo})
    pwlog("worker start sid=%s pid=%d titulo=%s rss=%dMB texto=%r"
          % (sid, os.getpid(), titulo, _rss_mb(), text[:60]))
    try:
        c = zc_proto.make(cwd=ws,
                          on_user_input=_user_input_handler(cfg, sid, titulo),
                          on_permission=lambda params: (
                               pwlog("🔐 permiso en headless: %s"
                                     % json.dumps(params, ensure_ascii=False)[:200]),
                               send(cfg, "🔐 «%s» pidió un permiso headless "
                                         "(debería correr yolo — avisame para "
                                         "revisarlo): <code>%s</code>"
                                         % (esc(titulo), esc(json.dumps(
                                             params, ensure_ascii=False)[:120])))))
    except OSError as e:
        WORKERS.pop(key)  # OJO key: pop(None) dejaba el orphan "nueva" (bug F0)
        send(cfg, "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120]))
        return
    # F16: /stop pasa por marker (state/bot-stop/<sid>) — el ProtoClient
    # ya no se comparte con el poller (los pipes no cruzan procesos)
    snap_sel = None  # /stop puede frenar ANTES de la asignación (finally la lee)
    stop_ya = []

    def stop_hook():
        """F16: el gancho de _turn_end_poll — si el poller dejó marker de
        /stop, frenar con el runtime PROPIO del worker (stop real)."""
        if stop_ya or not _stop_requested(sid):
            return
        stop_ya.append(1)
        pwlog("stop marker: session/stop %s" % sid)
        try:
            c.request("session/stop", {"sessionId": sid}, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    try:
        if not sid:  # /task new: crear la sesión AHORA (vive durante el turno)
            sid, err = _create_session(c, ws)
            if err:
                send(cfg, err)
                return
            act = dict(act, sid=sid)
            set_active_bot_session(act)
            WORKERS.rekey("nueva", sid)  # el botón de /stop apunta al sid real
        modelo = act.get("model")
        snap_sel = None if sid in (None, "nueva") else _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot: nada que restaurar
        err = _proto_ready(c, sid, model_sel=modelo, mode=act.get("mode"))
        if err:
            send(cfg, err)
            return
        if _stop_requested(sid):
            # /stop llegó durante el arranque: ni mandar el turno
            send(cfg, "⏹️ «%s» frenado por /stop (antes de enviar)." % esc(titulo))
            return
        pre = _fetch_messages(c, sid, limit=1)
        last_id = _last_id_of(pre)
        t0 = time.time()
        r = c.request("session/send",
                      {"sessionId": sid, "content": _headless_text(text, cfg),
                       "modelSelection": modelo or MODEL_SEL}, timeout=120)
        pwlog("send resp:", json.dumps(r)[:200])
        if "error" in r:
            send(cfg, "❌ send: " + esc(json.dumps(r["error"])[:150]))
            return
        t_send = time.time()  # F20: ancla del turnId PROPIO (turno cruzado)
        _active_turn_register(sid, titulo, t0, ws=ws, last_id=last_id)
        # Vista en vivo por delta stream: suscribir ANTES del send para
        # captar el razonamiento desde el arranque (reasoning_delta).
        ph = None
        if cfg.get("live_progress", True):
            ph = _live_send(cfg, "🧠 <b>«%s»</b> arrancando…" % esc(titulo))
            if ph:
                try:
                    c.request("session/subscribe",
                              {"sessionId": sid,
                               "deliveryKind": "desktop-continuous"}, timeout=15)
                except Exception:  # noqa: BLE001
                    pass
        if ph:
            res = {}
            th = _th.Thread(target=lambda: res.update(
                fin=_turn_end_poll(sid, t0, timeout=1800,
                                   stop_check=stop_hook, t_send=t_send)),
                daemon=True)
            th.start()
            lr = _live_view(c, cfg, sid, t0, titulo, ph, th, last_id=last_id)
            live_cot = (lr[0] if lr else "") or ""
            live_box = (lr[3] if lr else None) or ph  # caja viva (abajo si hubo CoT)
            fin = res.get("fin")
            if fin is None:  # la vista cortó por deadline: esperar el resto
                fin = _turn_end_poll(sid, t0, timeout=600, stop_check=stop_hook,
                                     t_send=t_send)
        else:
            fin = _turn_end_poll(sid, t0, timeout=1800, stop_check=stop_hook,
                                 t_send=t_send)
            live_cot = ""
            live_box = ph
        pwlog("fin:", (fin or {}).get("event"), "rss=%dMB rss_now=%dMB"
              % (_rss_mb(), _rss_now_mb()))
        REINTENTA = {"inline_keyboard": [[
            {"text": "🔁 Reintentar", "callback_data": "retry"}]]}
        if ph:
            if fin is None:
                _live_edit(cfg, live_box, "⏱️ <b>«%s»</b> — turno largo"
                           % esc(titulo))
            elif fin.get("event") == "turn.failed":
                _live_edit(cfg, live_box, "❌ <b>«%s»</b> falló — detalle abajo"
                           % esc(titulo))
            else:
                _live_edit(cfg, live_box, "✅ <b>«%s»</b> · respuesta:" % esc(titulo))
        if fin is None:
            # F21 — fin honesto: el turno REALMENTE sigue (el runtime vive,
            # el CLI sigue escribiendo al log). Antes el worker moría acá y
            # su finally MATABA el runtime con el mensaje "sigue de fondo"
            # en pantalla: mentira + turno muerto. Ahora:
            # 1) dejo el runtime VIVO (no lo cerramos en finally)
            # 2) registro el turno como huérfano: el supervisor del poller
            #    (_orphans_check) lo vigila y entrega la respuesta al final
            pwlog("abandono: fin=None tras 40min — runtime vivo, supervisor "
                  "asume la entrega")
            _orphans_handoff(cfg, sid, titulo, c, last_id, ph, live_box)
            raise _TurnAbandoned()  # salta al finally SIN cerrar el runtime
        if fin.get("event") == "turn.failed":
            err = str((fin.get("error") or {}).get("message", ""))[:150]
            send(cfg, "❌ «%s» falló: %s" % (esc(titulo), esc(err)),
                 markup=REINTENTA)
            return
        if WORKERS.stopped(sid):
            if ph:
                _live_edit(cfg, live_box, "⏹️ <b>«%s»</b> frenado" % esc(titulo))
            send(cfg, "⏹️ «%s» frenado por /stop." % esc(titulo))
            return
        dur = (time.time() - t0) / 60.0
        msgs = _fetch_messages(c, sid, after_id=last_id, limit=120) or []
        # F20: SOLO el último mensaje assistant — en sesión compartida con
        # la app hay respuestas ajenas en la ventana (incidente 18:29)
        texto = _extract_assistant_text(msgs, solo_ultima=True).strip()
        _turn_trace(cfg, titulo, sid, dur, msgs, live_cot=live_cot)
        if not texto:
            send(cfg, "✅ «%s» listo en %.0f min (sin texto de respuesta)." % (esc(titulo), dur))
            return
        _send_long_reply(cfg, titulo, dur, texto)
    except _TurnAbandoned:
        abandonado[0] = True
        # el mensaje al chat ya lo mandó _orphans_handoff; nada más acá
    except Exception as e:
        # un worker muerto NUNCA más es silencioso: avisar al chat + log
        pwlog("worker CRASH:", traceback.format_exc()[-500:])
        try:
            send(cfg, "⚠️ El worker de «%s» murió procesando tu mensaje: "
                  "<code>%s</code>" %
                  (esc(titulo), esc(str(e)[:150])), loud=True,
                  markup={"inline_keyboard": [[
                      {"text": "🔁 Reintentar", "callback_data": "retry"}]]})
        except Exception:
            pass
    finally:
        if abandonado[0]:
            # F21: el registro de turno activo QUEDA (es la señal del
            # supervisor); el runtime no se toca; la cola la maneja el
            # supervisor al fin real del turno.
            pwlog("worker out (runtime heredado al supervisor)")
            return
        _active_turn_clear(sid)
        WORKERS.pop(sid or "nueva")
        if sid:
            _stop_consume(sid)  # F16: marker consumido — no frenar el próximo turno
        try:
            c.close()
        except Exception as e:
            log("errors", "worker close: %s" % e)
        if sid and snap_sel:
            try:
                _restore_model(sid, snap_sel)
            except Exception as e:
                log("errors", "worker restore: %s" % e)
        try:
            _queue_drain(cfg, sid)
        except Exception:
            pass