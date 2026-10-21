"""config — LA fuente única de paths y configuración del bot (F1e).

Todos los módulos leen los paths COMO ATRIBUTO (config.STATE_DIR) en
call-time: así el fixture de tests rebindea UNA vez acá y desaparece la
divergencia gateway-vs-utils (el bug histórico: el gateway leía el
fixture y utils leía producción). Nada debe hacer `from config import
STATE_DIR` (copia el valor en el import — esa copia es la divergencia).
"""
import json
import os
import re

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".zcode")
CONFIG_PATH = os.environ.get("ZT_CONFIG", os.path.join(BASE, "telegram-remote.json"))
STATE_DIR = os.path.join(BASE, "hooks", "state")
LOG_DIR = os.path.join(BASE, "hooks", "logs")
DEFAULT_WS = os.path.join(BASE, "workspace", "default")
API = os.environ.get("ZT_API_BASE", "https://api.telegram.org")


# --- F34b: nombres de archivo del estado compartido (una fuente) ---
# El patrón es `_store(config.ARCH_X)` / `os.path.join(config.STATE_DIR,
# config.ARCH_X)`: un rename hoy es tocar ESTA lista, no cazar strings.
ARCH_WORKERS = "bot-workers.json"
ARCH_COLA = "bot-queue.json"
ARCH_SESION = "bot-session.json"
ARCH_AUDITORIA = "bot-auditoria.json"
ARCH_LIVE_SKIP = "bot-live-skip.json"
ARCH_RELOAD = "reload-now"
ARCH_ULTIMA_RESPUESTA = "last-reply.txt"
ARCH_ULTIMO_TRACE = "last-turn-trace.txt"

# R0b/F51c: cada cuánto avisar «⏱️ lleva N min» en un turno largo. La
# leen el worker (turnos del celu, workers._correr_turno vía live_view)
# y el vigilante de la Mac (razonamiento._vigilar_turno) con el MISMO
# valor: el silencio de un turno que se estira se leía como «se tildó»
# (Damian, noche del 20/10 y mañana del 21/10).
TURNO_LARGO_AVISO_S = 1800


def marker_path(session_id):
    """Marker del turno en vuelo (la definición vivía en hooks; mudada
    acá para que el nombre sea de UNO solo — gateway y espectador la
    importan igual)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "ses")
    return os.path.join(STATE_DIR, "turn-%s.marker" % safe)


def stop_dir():
    """Directorio de markers de /stop."""
    return os.path.join(STATE_DIR, "bot-stop")


def rollout_path(sid):
    """Model-io jsonl de una sesión (F31 /ctx, F33 espectador)."""
    return os.path.join(BASE, "cli", "rollout", "model-io-%s.jsonl" % sid)


def code_stamp():
    """F48d — sello del código instalado: mtime del paquete (deploy.sh lo
    reemplaza entero, rm -rf + cp → cambia). Los procesos CALIENTES (worker
    warm, vigilante de la Mac) lo comparan con el de su arranque y se
    retiran al verlo cambiar, así el próximo turno arranca frío con el
    código nuevo (antes: hasta 10 min de poller nuevo + worker viejo por
    deploy, 20/10 20:37-20:56). None si el paquete no está (la ventana
    entre el rm y el cp): cuenta como cambio."""
    try:
        return os.path.getmtime(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "__init__.py"))
    except OSError:
        return None


CODE_STAMP_INICIO = code_stamp()


def codigo_cambio():
    """¿Hubo un deploy desde que este proceso arrancó?"""
    return code_stamp() != CODE_STAMP_INICIO

DEFAULTS = {
    "token": "",
    "chat_id": None,
    "owner_user_id": None,
    "owner_name": "",
    "wait_timeout_s": 240,
    "question_wait_timeout_s": 60,
    "question_mode": "mac",
    "stuck_after_min": 20,
    "loud_all": True,
    "suppress_error_patterns": [
        "File has not been read yet",
        "File does not exist",
        "File has been (?:unexpectedly )?modified",
        "String to replace not found",
        "Found \\d+ matches? of the string to replace",
    ],
    "notify_prompt": True,
    "notify_stop": True,
    "notify_failures": True,
    "notify_session": True,
    "only_active_task": True,
    "enabled": True,
    # F8 (ADR-014): worker caliente — el runtime espera el próximo prompt
    # de la sesión durante estos segundos (0 = un proceso por turno, como
    # siempre). Y rotación: transcripto (MB) que dispara compact+resumen+
    # sesión nueva sembrada (0 = off). Ver workers._warm_wait/_rotar_sesion.
    "warm_keepalive_s": 600,
    "rotar_umbral_mb": 8,
    # F9: días que se conserva el transcripto (rollout model-io jsonl) de
    # sesiones que ya no referencia el bot (rotadas/archivadas) — el
    # archivo queda atrás tras cada rotación y crece ~un transcripto por
    # vez (44MB medidos). 0 = off.
    "rollout_retain_dias": 7,
    # F30: auditor de invariantes — cadencia del pase (0 = apagado),
    # fallos del mismo tipo+sesión en 10 min que disparan la alerta de
    # "sesión enferma", y minutos de cooldown por alerta (anti-spam).
    "auditor_cada_s": 30,
    "auditor_fallos_min": 2,
    "auditor_alerta_cooldown_min": 60,
    # F33: espectador del turno de la Mac (tail del rollout → Telegram);
    # apagar acá mata el feature entero (cot_stream/tools_stream siguen)
    "mac_espectador": True,
    "mac_cot_botones": True,  # F40: al cerrar un turno de la Mac, botones CoT n con su razonamiento
    # F44: el vigilante --cot-vivo queda CALIENTE esperando el próximo prompt
    # de la misma sesión de la Mac (un resume por sesión, no por turno —
    # la primera cajita pasa de ~48s a segundos). 0 = un vigilante por turno.
    "cot_vivo_keepalive_s": 600,
    # F7 streaming del turno: CoT en cajas y cajas de tools (falso = callado)
    "cot_stream": True,

    "cot_botones": True,  # F42: el CoT del celu como botones que crecen por fase (reemplaza el texto de cot_stream)
    # F45: un turno que cierra SIN razonamiento lo dice en una línea (las
    # tres fuentes: celu, Mac en vivo, captura al cierre). GLM-5.3 razona
    # de forma adaptativa y con el contexto largo suele saltearlo — sin el
    # aviso, cero cajitas era indistinguible de un bug.
    "cot_aviso_vacio": True,
    "tools_stream": True,
    "tools_caja_filtro": "accion",  # todas | accion | minimo (messages)
    "live_progress": True,          # caja viva de progreso (F7)
    "trace_documents": False,       # /cot: guardar documento del turno
    "headless_guard": True,         # guardrail anti-suicidio (F4d)
    # ADR-011: endpoint del webhook (los escribe /setwebhook)
    "webhook_url": "",
    "webhook_secret": "",
    # ADR-011: transporte de updates, mutuamente excluyentes
    "transport": "polling",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    # tmp+replace (auditoría profunda 20/10): un crash a mitad del
    # json.dump dejaba el archivo truncado → load_config caía en silencio
    # a DEFAULTS (token vacío) → bot sordo SIN ningún aviso
    tmp = "%s.tmp.%d" % (CONFIG_PATH, os.getpid())
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def configured(cfg):
    return bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))
