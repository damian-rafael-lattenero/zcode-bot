"""workers — los turnos headless del bot (F4g).

La cola (bot-queue.json), el spawn de workers como PROCESOS detached
(F16), prompt_worker (el corazón: sesión → runtime → live view →
entrega), el handoff de huérfanos y bot_stop. El prompt_worker sigue
siendo PROCESO separado (aislamiento real ante /nuclear); el spawn es
subprocess directo. Transporte POR ATRIBUTO (uhttp.*).
"""
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict

from zcode_bot import config
from zcode_bot import proto as zc_proto
from zcode_bot import cli_logs as zc_logs
from zcode_bot.core import (BotSession, active_bot_session,
                            set_active_bot_session,
                            _store, _workers, _questions,
                            _stop_request, _stop_requested, _stop_consume,
                            _task_current_ok,
                            _perm_key, _perm_replay, _perm_remember,
                            _model_snapshot, _restore_model, _proto_ready,
                            _create_session, _save_last_prompt,
                            running_turns, _queue_peek, _queue_sids_with_items)
from zcode_bot.live_view import (LiveTurnView, _turn_end_poll, _headless_text,
                                 _rss_mb, _rss_now_mb)
from zcode_bot.messages import (_fetch_messages, _extract_assistant_text,
                                _last_id_of)
from zcode_bot.reporting import _turn_trace, _send_long_reply
from zcode_bot.nuclear import _nuclear_active
from zcode_bot.models import MODEL_SEL
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log, pwlog


@dataclass
class QueueItem:
    """Un prompt encolado (bot-queue.json). El archivo persiste el shape
    histórico {"act", "text"} (to_dict); en memoria es tipada con compat
    mapping — item["text"] e item.get("act") siguen andando."""
    act: Dict[str, Any] = None
    text: str = ""

    def to_dict(self):
        return {"act": self.act or {}, "text": self.text}

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(act=d.get("act") if isinstance(d.get("act"), dict) else {},
                   text=str(d.get("text") or ""))

    def keys(self):
        return ("act", "text")

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def get(self, k, default=None):
        return getattr(self, k, default)


@dataclass
class WorkerEntry:
    """Registro de un worker vivo (state/bot-workers.json, flock). Lo
    construye prompt_worker y lo persiste con to_dict(); los lectores
    (status, nuclear, stop) siguen con .get por compat mapping."""
    t0: float = 0.0
    prompt: str = ""
    ws: str = ""
    titulo: str = ""
    pid: int = 0

    def to_dict(self):
        return {"t0": self.t0, "prompt": self.prompt, "ws": self.ws,
                "titulo": self.titulo, "pid": self.pid}

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(t0=float(d.get("t0") or 0.0),
                   prompt=str(d.get("prompt") or ""),
                   ws=str(d.get("ws") or ""),
                   titulo=str(d.get("titulo") or ""),
                   pid=int(d.get("pid") or 0))

    def get(self, k, default=None):
        return getattr(self, k, default)


def bot_stop(sid, titulo=None):
    """Frena una corrida iniciada DESDE el celular Y limpia su cola
    (pedido F17: /stop también descarta los mensajes encolados de esa
    task). F16: el frenado va por marker — el WORKER lo ve en su loop y
    ejecuta session/stop con su propio runtime (stop real)."""
    n = len(_queue_take(sid))
    tit = _workers().flag_stop(sid)
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


def _q_gemela(sid_eff, huella):
    """rid de la pregunta IDÉNTICA ya pendiente en el canal (mismo sid y
    mismas questions → misma huella): el re-disparo del CLI la reusa sin
    re-mostrar los botones. None si no hay gemela."""
    for q in _questions().pendings():
        if q.get("sid") == sid_eff and _perm_key(
                sid_eff, "AskUserQuestion",
                {"questions": q.get("questions") or []}) == huella:
            return q.get("request_id")
    return None


def _user_input_handler(cfg, sid, titulo):
    """Callback de interaction/requestUserInput: manda la ❓ al chat y
    espera la respuesta por ARCHIVO (F16: el poller la escribe al tocar
    el botón/número — no hay memoria compartida entre procesos). F15: la
    pregunta lleva SU sid — con varias tasks la respuesta enruta a la que
    preguntó (handle_free_text prioriza la task current)."""
    def handler(params):
        qs = params.get("questions") or []
        sid_eff = sid or (active_bot_session().get("sid"))
        # FIX 14/10 v2 (cuestionarios en loop): el CLI RE-DISPARA la misma
        # AskUserQuestion y el handler re-creaba la entrada y re-mostraba
        # los botones — Damian elegía la opción y volvía a ver la pregunta.
        # (El replay F27 solo cubría el hook del poller, no este camino.)
        # Capas: (1) respondida hace <10 min → devolver directo; (2)
        # idéntica pendiente → reusar su rid SIN re-mostrar; (3) nueva →
        # mostrar.
        huella = _perm_key(sid_eff, "AskUserQuestion", {"questions": qs})
        previa = _perm_replay(huella)
        if previa is not None:
            return {"action": "accept", "content": {"answer": previa}}
        rid = _q_gemela(sid_eff, huella)
        if rid is None:
            rid = str(params.get("requestId") or time.time())
            # /task new: el sid real nace después del spawn — para esa
            # ventana vale el que fija el worker como sesión activa
            _questions().ask(rid, qs, titulo, sid_eff)
            lines = ["❓ <b>%s</b>" % esc(titulo)]
            kb = []
            if len(qs) == 1:
                # una sola pregunta: los botones mapean 1:1 con las opciones
                # que usa la resolución por número (handle_free_text usa qs[0])
                for i, op in enumerate(qs[0].get("options") or [], 1):
                    d = (" — " + esc(op["description"][:60])) if op.get("description") else ""
                    lines.append("  %d. %s%s" % (i, esc(op.get("label", "?")), d))
                    kb.append([{"text": ("%d. %s" % (i, (op.get("label") or "?")))[:36],
                                "callback_data": "ans:%d" % i}])
            else:
                for q in qs:
                    if q.get("question"):
                        lines.append(esc(q["question"]))
                    for i, op in enumerate(q.get("options") or [], 1):
                        d = (" — " + esc(op["description"][:60])) if op.get("description") else ""
                        lines.append("  %d. %s%s" % (i, esc(op.get("label", "?")), d))
            lines.append("\nRespondé con el número (o tocá un botón)." if kb
                         else "\nRespondé con el número (o texto libre).")
            uhttp.send(cfg, "\n".join(lines), loud=True,
                 markup={"inline_keyboard": kb} if kb else None)
        resp = _questions().wait(rid, timeout=480)
        if resp is not None:
            _perm_remember(huella, resp)  # el re-disparo no vuelve a preguntar
            return {"action": "accept",
                    "content": {"answer": resp}}
        uhttp.send(cfg, "⌛️ La pregunta de «%s» caducó sin respuesta." % esc(titulo))
        return {"action": "decline", "reason": "timeout telegram"}
    return handler


def _queue_put(sid, act, text):
    if isinstance(act, BotSession):
        act = act.to_dict()  # la cola persiste dicts (QueueItem en F4g)
    def add(q):
        q = q if isinstance(q, dict) else {}
        lst = q.get(sid) or []
        lst.append(QueueItem(act, text[:6000]).to_dict())
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
    return [QueueItem.from_dict(e) for e in taken[0]]


def _queue_drain(cfg, sid):
    """Al terminar un turno, si quedaron prompts encolados para la sesión,
    lanza el siguiente (cadena). F17: si la sesión SIGUE ocupada (corrida
    de la Mac), la cola queda DORMIDA — sin este guard el relay
    re-spawneaba en loop mientras la task estaba ocupada (incidente
    18:03: 5 workers en 5 s, cada uno con su runtime). F15: si dejó de
    ser la task current, también duerme (la retoma /task)."""
    if not sid:
        return
    if _nuclear_active():
        pwlog("drain skip: nuclear en curso (cola dormida: %d)"
              % len(_queue_peek(sid)))
        return
    if sid in running_turns():
        pwlog("drain skip: %s sigue ocupada (cola dormida: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    if not _task_current_ok(cfg, sid):
        pwlog("drain skip: %s no es la task current (cola dormida: %d)"
              % (sid, len(_queue_peek(sid))))
        return
    _queue_launch_next(cfg, sid)


def _queue_launch_next(cfg, sid):
    """Toma el próximo prompt encolado de la sesión y lanza su worker
    (F16: proceso detached — la cadena de cola sobrevive restarts del
    poller). Devuelve (item, quedan) o None si no había nada."""
    lst = _queue_take(sid)
    if not lst:
        return None
    item = lst[0]
    for resto in lst[1:]:  # los devolvemos a la cola
        _queue_put(sid, resto["act"], resto["text"])
    if _spawn_worker(cfg, item["act"], item["text"]) is None:
        _queue_put(sid, item["act"], item["text"])  # no perderlo
        return None
    return item, len(lst) - 1


def _queue_resume(cfg, sid, titulo=None):
    """F15 — auto-resume: al volver una task (con /task) que tenía prompts
    encolados, arranca el primero. Si ya hay un worker vivo de esa sesión,
    la cola sigue sola al terminar su turno."""
    if not sid:
        return None
    if _nuclear_active():
        pwlog("resume skip: nuclear en curso")
        return None
    if _workers().get(sid) is not None:
        n = len(_queue_peek(sid))
        if n:
            return ("⏳ Ya hay un turno corriendo en «%s» — tus %d encolados "
                    "corren al terminar." % (esc(titulo or sid[-8:]), n))
        return None
    res = _queue_launch_next(cfg, sid)
    if not res:
        return None
    quedan = res[1]
    return ("⏳ Retomé la cola de «%s»%s — trabajando…"
            % (esc(titulo or sid[-8:]),
               " (%d más encolados)" % quedan if quedan else ""))


def _active_turn_register(sid, titulo, t0, ws=None, last_id=None):
    """Registro persistente del turno en curso: si el worker muere SIN
    correr su finally (kill -9 / OOM), el supervisor del poller lo ve y
    entrega el final — nadie muere en silencio."""
    if not sid:
        return

    def add(q):
        q = q if isinstance(q, dict) else {}
        q[sid] = {"titulo": titulo, "t0": t0, "pid": os.getpid(),
                  "ws": ws, "last_id": last_id}
        return q
    try:
        _store("bot-active.json").update_cross_process(add, {})
    except OSError:
        pass


def _active_turn_clear(sid):
    if not sid:
        return

    def rm(q):
        q = q if isinstance(q, dict) else {}
        q.pop(sid, None)
        return q
    try:
        _store("bot-active.json").update_cross_process(rm, {})
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
    if isinstance(act, BotSession):
        act = act.to_dict()  # el mailbox por stdin viaja como dict plano
    me = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "zcode-telegram.py")  # shim: entrypoint de subprocesos
    if not os.path.exists(me):
        me = os.path.abspath(__file__)
    try:
        with open(os.path.join(config.LOG_DIR, "worker-stderr.log"), "a") as err:
            p = subprocess.Popen(
                [sys.executable, me, "--run-turn"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                start_new_session=True,
                cwd=act.get("ws") or os.path.expanduser("~"))
            p.stdin.write(json.dumps({"act": act, "text": text}).encode("utf-8"))
            p.stdin.close()
        return p
    except OSError as e:
        uhttp.send(cfg, "❌ No pude lanzar el worker: " + esc(str(e)[:120]))
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
    atexit.register(lambda: _workers().pop(sid or "nueva"))
    prompt_worker(load_config(), act, text)


def prompt_worker(cfg, act, text):
    """Corre un prompt headless en la conversación activa y empuja la
    respuesta a Telegram. Vive en un PROCESO (F16: actor)."""
    import threading as _th
    sid, ws, titulo = act.get("sid"), act.get("ws"), act.get("title", "nueva")
    key = sid or "nueva"
    abandonado = [False]  # F21: fin=None → el runtime se hereda al supervisor
    if sid and _workers().get(sid) is not None:
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
        uhttp.send(cfg, "⏳ La Mac está ocupada con «%s» — tu mensaje quedó en "
                  "la cola (%d en espera). Corre al terminar la corrida "
                  "(o /stop para limpiarla)."
                  % (esc(titulo), len(_queue_peek(sid))))
        return
    _workers().register(key, WorkerEntry(
        t0=time.time(), prompt=text[:80], ws=ws, titulo=titulo,
        pid=os.getpid()).to_dict())
    pwlog("worker start sid=%s pid=%d titulo=%s rss=%dMB texto=%r"
          % (sid, os.getpid(), titulo, _rss_mb(), text[:60]))
    try:
        c = zc_proto.make(cwd=ws,
                          on_user_input=_user_input_handler(cfg, sid, titulo),
                          on_permission=lambda params: (
                               pwlog("🔐 permiso en headless: %s"
                                     % json.dumps(params, ensure_ascii=False)[:200]),
                               uhttp.send(cfg, "🔐 «%s» pidió un permiso headless "
                                         "(debería correr yolo — avisame para "
                                         "revisarlo): <code>%s</code>"
                                         % (esc(titulo), esc(json.dumps(
                                             params, ensure_ascii=False)[:120])))))
    except OSError as e:
        _workers().pop(key)  # OJO key: pop(None) dejaba el orphan "nueva" (bug F0)
        uhttp.send(cfg, "❌ No pude lanzar el runtime headless: " + esc(str(e)[:120]))
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
                uhttp.send(cfg, err)
                return
            act = dict(act, sid=sid)
            set_active_bot_session(act)
            _workers().rekey("nueva", sid)  # el botón de /stop apunta al sid real
        modelo = act.get("model")
        snap_sel = None if sid in (None, "nueva") else _model_snapshot(sid)
        if snap_sel and "zai-bot" in snap_sel:
            snap_sel = None  # ya era del bot: nada que restaurar
        err = _proto_ready(c, sid, model_sel=modelo, mode=act.get("mode"))
        if err:
            uhttp.send(cfg, err)
            return
        if _stop_requested(sid):
            # /stop llegó durante el arranque: ni mandar el turno
            uhttp.send(cfg, "⏹️ «%s» frenado por /stop (antes de enviar)." % esc(titulo))
            return
        pre = _fetch_messages(c, sid, limit=1)
        last_id = _last_id_of(pre)
        t0 = time.time()
        r = c.request("session/send",
                      {"sessionId": sid, "content": _headless_text(text, cfg),
                       "modelSelection": modelo or MODEL_SEL}, timeout=120)
        pwlog("send resp:", json.dumps(r)[:200])
        if e := zc_proto.proto_err(r, "send"):
            uhttp.send(cfg, e)
            return
        t_send = time.time()  # F20: ancla del turnId PROPIO (turno cruzado)
        _active_turn_register(sid, titulo, t0, ws=ws, last_id=last_id)
        # Vista en vivo por delta stream: suscribir ANTES del send para
        # captar el razonamiento desde el arranque (reasoning_delta).
        ph = None
        if cfg.get("live_progress", True):
            ph = uhttp._live_send(cfg, "🧠 <b>«%s»</b> arrancando…" % esc(titulo))
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
            lr = LiveTurnView(c, cfg, sid, t0, titulo, ph, th,
                               last_id=last_id).run()
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
                uhttp._live_edit(cfg, live_box, "⏱️ <b>«%s»</b> — turno largo"
                           % esc(titulo))
            elif fin.get("event") == "turn.failed":
                uhttp._live_edit(cfg, live_box, "❌ <b>«%s»</b> falló — detalle abajo"
                           % esc(titulo))
            else:
                uhttp._live_edit(cfg, live_box, "✅ <b>«%s»</b> · respuesta:" % esc(titulo))
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
            uhttp.send(cfg, "❌ «%s» falló: %s" % (esc(titulo), esc(err)),
                 markup=REINTENTA)
            return
        if _workers().stopped(sid):
            if ph:
                uhttp._live_edit(cfg, live_box, "⏹️ <b>«%s»</b> frenado" % esc(titulo))
            uhttp.send(cfg, "⏹️ «%s» frenado por /stop." % esc(titulo))
            return
        dur = (time.time() - t0) / 60.0
        msgs = _fetch_messages(c, sid, after_id=last_id, limit=120) or []
        # F20: SOLO el último mensaje assistant — en sesión compartida con
        # la app hay respuestas ajenas en la ventana (incidente 18:29)
        texto = _extract_assistant_text(msgs, solo_ultima=True).strip()
        _turn_trace(cfg, titulo, sid, dur, msgs, live_cot=live_cot)
        if not texto:
            uhttp.send(cfg, "✅ «%s» listo en %.0f min (sin texto de respuesta)." % (esc(titulo), dur))
            return
        _send_long_reply(cfg, titulo, dur, texto)
    except _TurnAbandoned:
        abandonado[0] = True
        # el mensaje al chat ya lo mandó _orphans_handoff; nada más acá
    except Exception as e:
        # un worker muerto NUNCA más es silencioso: avisar al chat + log
        import traceback
        pwlog("worker CRASH:", traceback.format_exc()[-500:])
        try:
            uhttp.send(cfg, "⚠️ El worker de «%s» murió procesando tu mensaje: "
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
        _workers().pop(sid or "nueva")
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


def _runtime_muerto(c):
    """True si el runtime hijo del worker ya no existe (OOM/kill)."""
    p = getattr(c, "p", None)
    try:
        return p is not None and p.poll() is not None
    except OSError:
        return True


class _TurnAbandoned(Exception):
    """F21: fin=None tras 40 min — el turno real sigue pero el worker
    entrega el runtime al supervisor (nunca matarlo ni mentir)."""


def _orphan_fetch_text(ent, sid):
    """F21: texto de la respuesta de un turno huérfano ya completado.
    Runtime efímero: resume del sid y fetch — SIN setModel/setMode para
    no pisar la configuración de la sesión. Nunca lanza."""
    try:
        c = zc_proto.make(cwd=ent.get("ws") or config.DEFAULT_WS)
        try:
            r = c.request("session/resume", {"sessionId": sid}, timeout=45)
            if "error" in r:
                return None
            msgs = _fetch_messages(c, sid, after_id=ent.get("last_id"),
                                   limit=120) or []
            return _extract_assistant_text(msgs, solo_ultima=True).strip()
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None


def _orphans_handoff(cfg, sid, titulo, c, last_id, ph, live_box):
    """F21: registro del turno activo para el supervisor del poller. El
    runtime queda VIVO (proceso worker lo pierde al salir, pero el runtime
    es hijo del worker... OJO: al morir el worker, el runtime queda
    huérfano PERO SIGUIENDO — el CLI escribe al log del CLI, que es lo
    único que el supervisor necesita leer. La entrega del texto la hace
    _orphans_check al ver turn.completed)."""
    _active_turn_register(sid, titulo, time.time())
    if ph:
        try:
            uhttp._live_edit(cfg, live_box,
                       "⏱️ <b>«%s»</b> — turno largo: sigue corriendo y te "
                       "aviso al terminar (supervisión activada)"
                       % esc(titulo))
        except Exception:  # noqa: BLE001
            pass
    uhttp.send(cfg, "⏱️ «%s» lleva 40 min corriendo — sigo supervisándolo y te "
              "entrego la respuesta apenas termine." % esc(titulo))


def _orphans_check(cfg):
    """F21 — supervisor de huérfanos (corre en cada vuelta del poller):
    turnos del bot cuyo worker murió abruptamente. El finally del worker
    siempre limpia su registro → entrada con pid muerto = muerte SIN
    cleanup → aviso honesto en vez de silencio eterno. Barato: un read +
    un os.kill por entrada."""
    q = _store("bot-active.json").read()
    if not isinstance(q, dict) or not q:
        return
    ahora = time.time()
    for sid, ent in list(q.items()):
        if not isinstance(ent, dict):
            continue
        t0 = float(ent.get("t0") or 0)
        if ahora - t0 > 7200:  # 2h: purge silencioso (anti PID-reuse)
            _active_turn_clear(sid)
            continue
        pid = ent.get("pid")
        try:
            os.kill(int(pid), 0)
            continue  # vivo: worker sano, nada que hacer
        except (OSError, ValueError, TypeError):
            pass
        # pid muerto: ¿llegó a terminar el turno antes de caer el worker?
        fin = None
        for _ts, _s, _ev, d in zc_logs.iter_events(
                config.BASE, needles=("turn.completed", "turn.failed"),
                sids={sid}, since_epoch=t0, last_files=1,
                tail_bytes=512 * 1024):
            fin = d
        titulo = ent.get("titulo") or sid[-8:]
        if fin is not None and fin.get("event") == "turn.completed":
            # entrega REAL del texto: runtime efímero de solo-lectura
            # (resume del sid SIN setModel — no pisa la configuración)
            texto = _orphan_fetch_text(ent, sid)
            if texto:
                _send_long_reply(
                    cfg, titulo, (ahora - t0) / 60.0, texto)
                pwlog("orphan", sid[-8:], "entregado (%d chars)" % len(texto))
            else:
                uhttp.send(cfg, "🛟 El worker de «%s» murió, pero su turno "
                      "TERMINÓ — la respuesta quedó en la sesión (/cot "
                      "para el trace)." % esc(titulo))
                pwlog("orphan", sid[-8:], "fin=completed (texto no recuperable)")
        elif fin is not None:
            uhttp.send(cfg, "❌ «%s» falló (el worker había muerto): %s"
                 % (esc(titulo), esc(str((fin.get("error") or {})
                                         .get("message", ""))[:150])))
            pwlog("orphan", sid[-8:], "fin=failed")
        else:
            uhttp.send(cfg, "⛔️ El turno de «%s» murió con su worker (kill/OOM) — "
                  "no va a llegar respuesta. Reenviá el mensaje cuando "
                  "quieras." % esc(titulo))
            pwlog("orphan", sid[-8:], "sin fin (runtime muerto)")
        _active_turn_clear(sid)
        # la cola de esa sesión quedó dormida: despertarla si es la current
        try:
            if _task_current_ok(cfg, sid):
                _queue_resume(cfg, sid, titulo)
        except Exception:  # noqa: BLE001
            pass
