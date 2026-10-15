"""core — seam compartido del bot (F4b).

El desmembramiento del gateway encontró 10 ciclos de dependencia entre
los dominios (reports ↔ workers ↔ hooks ↔ commands...). Este módulo es
el CORTE RAÍZ: las funciones de estado y sesión que todos comparten
viven acá y core no depende de ningún otro dominio del bot.

Regla de la casa (F1e/F4a): los paths se resuelven SIEMPRE por atributo
(config.STATE_DIR) en call-time — nunca se hornean al importar.
"""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, NamedTuple, Optional

from zcode_bot import config
from zcode_bot import state as zc_state
from zcode_bot import concurrency as zc_conc
from zcode_bot import proto as zc_proto
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import cli_logs as zc_logs
from zcode_bot import fswatch
from zcode_bot.models import MODEL_SEL
from zcode_bot.utils.validation import first
from zcode_bot.utils.formatting import preview
from zcode_bot.utils.logging import log


def _store(name):
    """AtomicStore para un archivo de state (path resuelto EN EL MOMENTO —
    así los tests pueden parchear config.STATE_DIR después del import)."""
    return zc_state.AtomicStore(os.path.join(config.STATE_DIR, name))


# F16: registry de workers en ARCHIVO (state/bot-workers.json, flock) —
# la misma vista para poller y workers-proceso; un restart del poller no
# borra los turnos en curso (era: threads daemon con estado en memoria).
# F16: preguntas ❓ por archivo (state/bot-q/) — la respuesta del chat
# cruza del poller al proceso worker sin memoria compartida.
# F4a: accessors con path resuelto EN EL MOMENTO (cacheado por path:
# fixture y producción nunca colisionan). Antes eran globals que horneaban
# el path al importar — el fixture de tests tenía que rebindearlas.
_WORKERS_CACHE = {}
_QUESTIONS_CACHE = {}


def _workers():
    path = os.path.join(config.STATE_DIR, "bot-workers.json")
    w = _WORKERS_CACHE.get(path)
    if w is None:
        w = _WORKERS_CACHE[path] = zc_conc.WorkersRegistry(path)
    return w


def _questions():
    path = os.path.join(config.STATE_DIR, "bot-q")
    q = _QUESTIONS_CACHE.get(path)
    if q is None:
        q = _QUESTIONS_CACHE[path] = zc_conc.QuestionFiles(path)
    return q


# ------------------------------------------------------------- entidades

@dataclass
class BotSession:
    """Conversación activa del bot (state/bot-session.json).

    F22: model y mode SOBREVIVEN el /task switch — solo van al JSON si
    existen (to_dict preserva el shape histórico del archivo).
    Compat mapping (get/keys/__getitem__): los consumidores viejos
    (act.get("sid"), dict(act, mode=...)) siguen andando mientras migran
    a atributos.
    """
    sid: str = ""
    title: str = ""
    ws: str = ""
    model: Optional[Dict[str, Any]] = None
    mode: Optional[str] = None

    @classmethod
    def from_dict(cls, d):
        d = d if isinstance(d, dict) else {}
        return cls(sid=str(d.get("sid") or ""),
                   title=str(d.get("title") or ""),
                   ws=str(d.get("ws") or ""),
                   model=d.get("model") if isinstance(d.get("model"), dict) else None,
                   mode=d.get("mode") if d.get("mode") else None)

    def to_dict(self):
        d = {"sid": self.sid, "title": self.title, "ws": self.ws}
        if self.model is not None:
            d["model"] = self.model
        if self.mode is not None:
            d["mode"] = self.mode
        return d

    # --- compat mapping (transición F4b; los módulos nuevos usan atributos)
    def keys(self):
        return self.to_dict().keys()

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def get(self, k, default=None):
        return getattr(self, k, default)


class WaitReply(NamedTuple):
    """Respuesta del dueño a una espera (wait_reply): kind ∈ yes|no|text,
    text = lo tipeado. NamedTuple porque el retorno histórico era la tupla
    (kind, text) — los unpackings y los stubs de tests siguen intactos."""
    kind: Optional[str]
    text: Optional[str]


# ------------------------------------------------------------ sesión activa

def active_bot_session():
    return BotSession.from_dict(_store("bot-session.json").read())


def set_active_bot_session(d):
    if isinstance(d, BotSession):
        d = d.to_dict()
    try:
        _store("bot-session.json").write(d)
    except OSError as e:
        log("errors", "set_active_bot_session: %s" % e)


def active_workspace_path():
    """Workspace que muestra la ventana de la app (setting.json:
    lastWorkspaceSession[lastActiveTabIndex])."""
    try:
        s = json.load(open(os.path.join(config.BASE, "v2", "setting.json")))
        lws = s.get("lastWorkspaceSession") or []
        i = s.get("lastActiveTabIndex")
        if lws and isinstance(i, int) and 0 <= i < len(lws) and isinstance(lws[i], dict):
            return lws[i].get("workspacePath")
        if lws and isinstance(lws[0], dict):
            return lws[0].get("workspacePath")
    except (OSError, ValueError):
        pass
    return None


def session_title_from_db(sid):
    """Título de una sesión (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.session_title(config.BASE, sid)


# --------------------------------------- lecturas de estado compartido

def running_turns(max_age=7200):
    """Sesiones con un turno corriendo ahora, según el log del CLI
    (replay started/completed/failed sobre los últimos 2 logs)."""
    lim = time.time() - max_age
    out = {}
    for ts, sid, ev, _d in zc_logs.iter_events(
            config.BASE, needles=("turn.",), since_epoch=lim,
            last_files=2, tail_bytes=0):
        if ev == "turn.started":
            out[sid] = ts
        elif ev in ("turn.completed", "turn.failed"):
            out.pop(sid, None)
    return out


def _queue_peek(sid):
    q = _store("bot-queue.json").read()
    return (q.get(sid) or []) if isinstance(q, dict) else []


def _queue_sids_with_items():
    q = _store("bot-queue.json").read()
    if not isinstance(q, dict):
        return []
    return [sid for sid, lst in q.items() if lst]


def _hook_sid(payload):
    sid = first(payload, "session_id", "sessionId")
    return str(sid) if sid else None


def _task_current_ok(cfg, sid):
    """F15 — filtro anti-ruido: una sesión puede hacer push al chat solo si
    es la task current (bot-session.json leído EN VIVO en cada send: un
    /task switch corta el stream a mitad de turno). Las respuestas directas
    a comandos/mensajes del usuario NO pasan por acá. Sin sid activo no hay
    push de task (obliga a elegir con /task)."""
    if not cfg.get("only_active_task", True):
        return True
    if not sid:
        return False
    return sid == active_bot_session().get("sid")


def _worker_owned(sid):
    """F15 — sesión con worker del bot corriendo: el worker YA reporta su
    turno (caja viva, CoT, ✅/⏹️). Los hooks ▶️/✅/🔐/❓/🟢 de esa sesión
    solo duplicarían (doble-stream verificado: payloads de turno headless
    con el HEADLESS_GUARD disparando UserPromptSubmit+Stop además de la
    caja). Además un hook con wait_reply competiría por el offset."""
    return bool(sid) and _workers().get(sid) is not None


# ------------------------------------------------- selección de modelo/modo

def _cur_sel(act):
    """Model selection vigente de la conversación activa (dict sane con
    provider/modelId/options.reasoningLevel)."""
    sel = dict(MODEL_SEL)
    sel["options"] = dict(MODEL_SEL.get("options") or {})
    if isinstance(act.get("model"), dict):
        sel.update({k: v for k, v in act["model"].items() if k != "options"})
        sel["options"] = dict(act["model"].get("options") or sel["options"])
    return sel


def _set_sel(act, model_id=None, thinking=None):
    """Cambia modelo o thinking de la activa PRESERVANDO lo otro."""
    sel = _cur_sel(act)
    if model_id:
        sel["modelId"] = model_id
    if thinking:
        sel["options"] = dict(sel.get("options") or {}, reasoningLevel=thinking)
    set_active_bot_session(dict(act, model=sel))


def _model_snapshot(sid):
    """Última runtime/model_selection de la sesión (data JSON cruda) o None
    (Repository: zcode_bot.tasks_repo)."""
    return zc_repo.model_selection(config.BASE, sid)


def _restore_model(sid, snap):
    """Devuelve la selección a su estado previo (o la borra si no había):
    el setModel zai-bot del bot NO debe quedar pegado en sesiones de la app
    (su registry no conoce zai-bot y se traba el picker)."""
    import sqlite3
    try:
        db = sqlite3.connect(os.path.join(config.BASE, "cli", "db", "db.sqlite"))
        if snap is None:
            db.execute("DELETE FROM session_entry WHERE session_id=? "
                       "AND type='runtime/model_selection'", (sid,))
        else:
            db.execute("UPDATE session_entry SET data=? WHERE session_id=? "
                       "AND type='runtime/model_selection'", (snap, sid))
        db.commit()
        db.close()
    except sqlite3.Error as e:
        log("errors", "_restore_model(%s): %s — el model selection puede "
            "haber quedado en zai-bot" % (sid, e))


# ------------------------------------------------------ runtime del CLI

def transcript_mb(sid):
    """MB del transcripto de la sesión (rollout model-io jsonl): lo que
    paga CADA request en frío (resume/setModel/setMode devuelven el
    historial completo). compact NO lo achica (15/10: 4 compacts exitosos
    y el resume siguió en 27.5MB — achica el CONTEXTO del modelo, no el
    historial)."""
    p = os.path.join(config.BASE, "cli", "rollout",
                     "model-io-%s.jsonl" % sid)
    try:
        return os.path.getsize(p) / (1024.0 * 1024.0)
    except OSError:
        return 0.0


def _timeout_por_mb(sid):
    """Techo de espera para resume/setModel/setMode. Cada request sobre
    una sesión existente devuelve el historial COMPLETO (F7++), así que
    el techo escala con el transcripto. Incidente 18:10 (15/10): con
    timeout fijo de 30/60s, setModel/setMode vencían en una sesión de
    25MB (~30s por request bajo carga) → el turno moría en silencio y en
    BUCLE (el mode_set recién se registra tras un _proto_ready exitoso,
    así que el próximo turno volvía a pagar todo). Cálculo: 60s base +
    4s/MB (~1.1MB/s medido, 3x de margen por carga), tope 300s. Es un
    TECHO, no una espera: una sesión chica sigue respondiendo en segundos."""
    return min(300, 60 + int(transcript_mb(sid) * 4))


def _contexto_ktokens(sid):
    """Miles de tokens REALES del contexto: la última usage del model-io
    jsonl (lo que el modelo vio en el request más reciente). None si no
    hay dato (sin archivo o sin usage — p.ej. sesión nueva)."""
    p = os.path.join(config.BASE, "cli", "rollout",
                     "model-io-%s.jsonl" % sid)
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))  # la última usage vive al final
            cola = f.read().decode("utf-8", "replace")
        ultimas = [l for l in cola.splitlines() if '"usage"' in l]
        if not ultimas:
            return None
        d = json.loads(ultimas[-1]).get("response") or {}
        u = d.get("usage") or (((d.get("providerMetadata") or {})
                                .get("anthropic") or {}).get("usage") or {})
        if "inputTokens" in u:  # forma canónica (incluye cache read)
            return u["inputTokens"] / 1000.0
        # forma provider (snake_case: input y cache_read van separados)
        if "input_tokens" in u:
            return (u["input_tokens"]
                    + (u.get("cache_read_input_tokens") or 0)) / 1000.0
    except (OSError, ValueError, TypeError):
        return None
    return None


def _timeout_compact(sid):
    """Techo de espera para session/compact — regla lineal de Damian
    calibrada con lo medido: 1.2 s por 1k tokens de contexto (medido
    15/10: ~135k tokens → 103s real ≈ 0.76 s/ktok; 1.2 deja ~1.6x de
    margen). 100k → 120s, 300k → 360s; base 300 (el default de siempre,
    3x el compact más grande medido), tope 600 (10 min)."""
    ktok = _contexto_ktokens(sid)
    if not ktok:
        return 300
    return min(600, max(300, int(ktok * 1.2)))


def _proto_ready(c, sid, model_sel=None, mode=None, skip_model=False,
                 skip_mode=False):
    """resume + setModel + setMode. El bot SIEMPRE fuerza un modo explícito:
    sin modo guardado → yolo (decisión de diseño: los prompts remotos corren
    sin diálogos de permiso; una sesión en build/edit headless se queda sin
    nadie que apruebe y el agente termina pidiendo "reiniciá ZCode").
    Devuelve None o msg de error.
    F7+: skip_model/skip_mode saltan los sets NO-OP. Cada request sobre
    una sesión grande devuelve el historial COMPLETO en la respuesta
    (medido en la sesión F4+F7: 26MB / 3640 mensajes ≈ 30s POR request —
    el "tarda un minuto en arrancar") — los tres requests usan el techo
    por MB (incidente 18:10: los fijos de 30/60s vencían y el turno moría
    en bucle). El modelo se valida contra el sqlite autoritativo
    (runtime/model_selection); el modo contra mode_set, el registro de lo
    que ESTE bot seteó último."""
    t_req = _timeout_por_mb(sid)
    r = c.request("session/resume", {"sessionId": sid}, timeout=t_req)
    if e := zc_proto.proto_err(r, "resume"):
        return e
    if not skip_model:
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": model_sel or MODEL_SEL},
                      timeout=t_req)
        if e := zc_proto.proto_err(r, "setModel"):
            return e
    if not skip_mode:
        r = c.request("session/setMode",
                      {"sessionId": sid, "mode": mode or "yolo"},
                      timeout=t_req)
        if e := zc_proto.proto_err(r, "setMode"):
            return e
    return None


def _create_session(c, ws):
    """session/create; devuelve (sid, None) o (None, msg_error). La sesión
    vive mientras viva el cliente: crearla dentro del worker del turno."""
    r = c.request("session/create",
                  {"workspace": {"workspaceKey": ws, "workspacePath": ws}},
                  timeout=60)
    if e := zc_proto.proto_err(r, "create"):
        return None, e
    ses = ((r.get("result") or {}).get("session")) or {}
    sid = ses.get("sessionId") or ses.get("id")
    if not sid:
        return None, "❌ create: sin sessionId en la respuesta"
    return sid, None


def _log_watch_paths():
    """Paths a vigilar para saber que el CLI escribió eventos: los mismos
    archivos que escanea iter_events (últimos 2) + el dir (rotación)."""
    d = os.path.join(config.BASE, "cli", "log")
    out = [d]
    try:
        logs = sorted((f for f in os.listdir(d)
                       if f.startswith("zcode-") and f.endswith(".jsonl")),
                      reverse=True)
        out.extend(os.path.join(d, f) for f in logs[:2])
    except OSError:
        pass
    return out


def _wait_cli_event(sid, t0, eventos, timeout=200):
    """Espera REACTIVA+TTL en el log del CLI un evento de `sid` con
    ts >= t0: fswatch.wait_for duerme en el kernel hasta que el log
    cambie (mismos paths que _turn_end_poll) y re-chequea el predicado.
    Devuelve el dict del evento (el ÚLTIMO que matchee) o None al vencer
    el TTL."""
    fin = []

    def hay():
        fin.clear()
        for _ts, _s, _ev, d in zc_logs.iter_events(
                config.BASE, needles=tuple(eventos), sids={sid}, since_epoch=t0,
                last_files=1, tail_bytes=0):
            fin.append(d)
        return bool(fin)

    if not fswatch.wait_for(hay, _log_watch_paths(), timeout):
        return None
    return fin[-1]


# ------------------------------------------------------------ last prompt

def _last_prompt_path():
    return os.path.join(config.STATE_DIR, "last-prompt.json")


def _save_last_prompt(act, text):
    if isinstance(act, BotSession):
        act = act.to_dict()  # el store persiste dicts planos
    try:
        _store("last-prompt.json").write({"act": act, "text": text})
    except OSError:
        pass


def _load_last_prompt():
    d = _store("last-prompt.json").read()
    if isinstance(d, dict) and d.get("text"):
        return d.get("act") or {}, str(d["text"])
    return None, None


# ------------------------------------------- /stop por marker (F16)

def _stop_file(sid):
    d = os.path.join(config.STATE_DIR, "bot-stop")
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


# ----------------------------------------------- permisos: dedupe/replay

def _perm_key(sid, name, inp):
    """Huella estable de un pedido de permiso (para dedupe/replay)."""
    try:
        cuerpo = json.dumps(inp, sort_keys=True, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        cuerpo = preview(inp, 500)
    return "%s|%s|%s" % (sid or "?", name,
                         hashlib.sha1(cuerpo.encode("utf-8")).hexdigest()[:16])


def _perm_remember(key, decision):
    """Registra la decisión para replays (el CLI a veces re-dispara el
    mismo PermissionRequest; ver incidente 'apruebo y me lo pide otra
    vez' del 13/10)."""
    def fn(d):
        d = d if isinstance(d, dict) else {}
        d[key] = {"ts": time.time(), "decision": decision}
        # podar: quedarse con las últimas 50
        if len(d) > 50:
            for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:-50]:
                d.pop(k, None)
        return d
    try:
        _store("bot-perm.json").update_cross_process(fn, {})
    except OSError:
        pass


_PERM_REPLAY_S = 600  # misma huella a los <10 min → misma decisión, al toque


def _perm_replay(key):
    d = _store("bot-perm.json").read() or {}
    ent = d.get(key)
    if ent and time.time() - ent.get("ts", 0) < _PERM_REPLAY_S:
        return ent.get("decision")
    return None
