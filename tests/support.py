"""Soporte de tests: entorno ~/.zcode FAKE determinista + carga del módulo
con paths parcheados. Ningún test toca el ~/.zcode real ni la red.

Patrón: Test Double (fixture environment) — el módulo se carga via importlib
y se parchean sus constantes de path (BASE/STATE_DIR/...), porque el monolito
resuelve paths en call-time desde esas globales.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import time
from unittest import mock

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "zcode_bot",
                   "gateway.py")
SHIM = os.path.join(os.path.dirname(__file__), "..", "src",
                    "zcode-telegram.py")

# src/ en sys.path AL IMPORTAR support (no en load_module): el orden de
# imports entre archivos de test dejaba de ser determinista (lo destapó
# el runner paralelo: cada archivo en su propio proceso)
_src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# F34d: constantes REALES de src — un test que necesita un límite lo
# importa de acá en vez de re-hardcodearlo (cuando src cambia, el test
# sigue describiendo el mismo contrato)
from zcode_bot.limites import (CH_CAJA, CH_RESPUESTA, CH_TURNO, PARTES_MAX,  # noqa: E402
                               TELEGRAM_MSG_MAX)
from zcode_bot.audit import CAP as AUDIT_CAP  # noqa: E402
PID_FALSO = 424242  # pid falso canónico para auditar/orphans


def hacer_fake_tg_api(cap):
    """Mock canónico de tg_api: graba params por método en el dict dado
    (test_f3, test_f6_nuclear y test_f7_trace duplicaban la firma)."""
    def fake_tg_api(cfg, method, params=None, timeout=35):
        cap[method] = params
    return fake_tg_api


# Datos deterministas del fixture (fechas relativas a "ahora" solo donde el
# código las compara contra time.time(); los textos son fijos)
PROJ_BOT = "/fake/proj/bot"
PROJ_CV = "/fake/proj/cv"
PROJ_HS = "/fake/proj/haskell"
DEFAULT_WS = "/fake/.zcode/workspace/default"

T1 = "sess_TEST00000000000000000000001"  # testing (bot) — corriendo
T2 = "ses_TEST000000000000000000000002"  # CV — formato sid viejo (ses_)
T3 = "sess_TEST00000000000000000000003"  # MCP
T4 = "sess_TEST00000000000000000000004"  # instalaciones (sueltas, default ws)


def _iso(epoch_s):
    import datetime
    return datetime.datetime.fromtimestamp(epoch_s, datetime.timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_env(root):
    """Crea el árbol fake y devuelve dict con los paths clave."""
    now = time.time()
    env = {
        "base": root,
        "db": os.path.join(root, "cli", "db", "db.sqlite"),
        "idx": os.path.join(root, "v2", "tasks-index.sqlite"),
        "log_today": os.path.join(root, "cli", "log",
                                  "zcode-%s.jsonl" % time.strftime("%Y-%m-%d")),
        "log_yday": os.path.join(
            root, "cli", "log",
            "zcode-%s.jsonl" % time.strftime(
                "%Y-%m-%d", time.localtime(now - 90000))),
        "state": os.path.join(root, "hooks", "state"),
        "setting": os.path.join(root, "v2", "setting.json"),
        "rollout": os.path.join(root, "cli", "rollout"),
        "sessions": os.path.join(root, "v2", "sessions"),
    }
    for d in ("cli/db", "cli/log", "v2", env["state"], env["rollout"],
              env["sessions"]):
        os.makedirs(os.path.join(root, d), exist_ok=True)

    # --- db.sqlite: model_usage / session_entry / session
    db = sqlite3.connect(env["db"])
    db.execute("CREATE TABLE model_usage (session_id TEXT, input_tokens INT,"
               " output_tokens INT, model_id TEXT, started_at INT)")
    ms = int(now * 1000)
    rows = [
        (T1, 100000, 15412, "zai-bot/GLM-5.3", ms - 60000),
        (T1, 90000, 12000, "zai-bot/GLM-5.3", ms - 500000),
        (T2, 130000, 2200, "account:zai/GLM-5.3-Flash", ms - 3600000),
        (T3, 800, 200, "zai-bot/GLM-5.3", ms - 7200000),
        (T4, 16000, 0, "zai-bot/GLM-5.3", ms - 86400000),
    ]
    db.executemany("INSERT INTO model_usage VALUES (?,?,?,?,?)", rows)
    db.execute("CREATE TABLE session_entry (session_id TEXT, type TEXT,"
               " data TEXT, time_created INT)")
    db.execute("INSERT INTO session_entry VALUES (?,?,?,?)",
               (T2, "runtime/model_selection",
                json.dumps({"modelSelection": {"providerId": "account:zai",
                                               "modelId": "GLM-5.3-Flash",
                                               "options": {"reasoningLevel": "low"}}}),
                ms - 3600000))
    db.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT)")
    db.execute("INSERT INTO session VALUES (?,?)", (T4, "instalaciones-db"))
    db.execute("INSERT INTO session VALUES (?,?)", (T1, "testing"))
    db.execute("INSERT INTO session VALUES (?,?)", (T2, "CV"))
    db.execute("INSERT INTO session VALUES (?,?)", (T3, "MCP"))
    db.commit()
    db.close()

    # --- tasks-index.sqlite
    idx = sqlite3.connect(env["idx"])
    idx.execute("CREATE TABLE tasks (workspace_key TEXT, workspace_path TEXT,"
                " task_id TEXT, title TEXT, updated_at INT, pinned INT,"
                " archived INT, deleted INT)")
    trows = [
        (PROJ_BOT, PROJ_BOT, T1, "testing", ms - 60000, 0, 0, 0),
        ("hashlegac0001", PROJ_CV, T2, "CV", ms - 3600000, 0, 0, 0),  # legacy
        (PROJ_CV, PROJ_CV, T2, "CV", ms - 3600000, 0, 0, 0),
        (PROJ_HS, PROJ_HS, T3, "MCP", ms - 7200000, 1, 0, 0),
        (DEFAULT_WS, DEFAULT_WS, T4, "instalaciones", ms - 86400000, 0, 0, 0),
        (PROJ_BOT, PROJ_BOT, "sess_ARCHIVED", "borrada", ms, 0, 1, 0),
        (PROJ_BOT, PROJ_BOT, "sess_DELETED", "borrada2", ms, 0, 0, 1),
    ]
    idx.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", trows)
    idx.commit()
    idx.close()

    # --- logs del CLI (eventos turn.* y compact.*)
    with open(env["log_today"], "w") as f:
        f.write(json.dumps({"timestamp": _iso(now - 60), "event": "turn.started",
                            "sessionId": T1}) + "\n")
        f.write(json.dumps({"timestamp": _iso(now - 3600), "event": "turn.started",
                            "sessionId": T2}) + "\n")
        f.write(json.dumps({"timestamp": _iso(now - 3500), "event": "turn.completed",
                            "sessionId": T2}) + "\n")
        f.write(json.dumps({"timestamp": _iso(now - 120), "event": "compact.completed",
                            "sessionId": T1,
                            "context": {"durationMs": 144409}}) + "\n")
    with open(env["log_yday"], "w") as f:
        f.write(json.dumps({"timestamp": _iso(now - 90000), "event": "turn.started",
                            "sessionId": T4}) + "\n")
        f.write(json.dumps({"timestamp": _iso(now - 89000), "event": "turn.completed",
                            "sessionId": T4}) + "\n")

    # --- setting.json + state
    with open(env["setting"], "w") as f:
        json.dump({"lastWorkspaceSession": [{"workspacePath": PROJ_BOT}],
                   "lastActiveTabIndex": 0}, f)
    with open(os.path.join(env["state"], "bot-session.json"), "w") as f:
        json.dump({"sid": T1, "title": "testing", "ws": PROJ_BOT}, f)
    with open(os.path.join(env["state"], "tg-offset"), "w") as f:
        f.write("0")
    with open(os.path.join(env["state"], "last-reply.txt"), "w") as f:
        f.write("Última respuesta de ejemplo para /last.")
    return env


def load_module(env):
    """Carga el gateway y rebindea zcode_bot.config al fixture.

    Los paths SIEMPRE se leen como atributo (config.STATE_DIR) — un solo
    punto de rebind para gateway y utils (F1e: antes el gateway leía el
    fixture y utils leía producción — divergencia silenciosa)."""
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from zcode_bot import config as zc_config
    zc_config.BASE = env["base"]
    zc_config.STATE_DIR = env["state"]
    zc_config.LOG_DIR = os.path.join(env["base"], "hooks", "logs")
    zc_config.CONFIG_PATH = os.path.join(env["base"], "telegram-remote.json")
    zc_config.DEFAULT_WS = DEFAULT_WS
    spec = importlib.util.spec_from_file_location("zt_golden", SRC)
    zt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(zt)
    # F8: los knobs del worker caliente/rotación EN 0 para la suite — el
    # default de producción (600s/8MB) haría esperar al warm loop en cada
    # test de prompt_worker. Un test que los quiera, los pasa en su cfg.
    # F40: el spawn de --cot-botones en on_stop lo gatea cfg; load_config
    # lee DEFAULTS por nombre del módulo real (no esta copia zt): se apaga
    # EN EL DICT REAL para que ningún test lance subprocesos. Un test que lo
    # quiera, lo prende en su cfg.
    zc_config.DEFAULTS["mac_cot_botones"] = False
    zc_config.DEFAULTS["cot_botones"] = False  # F42: idem, caja de botones
    # F44: el vigilante caliente dormiría hasta 600s esperando el próximo
    # marker — en la suite, un turno y chau (un test que quiera la espera
    # pasa keepalive explícito con reloj/sleep fakes)
    zc_config.DEFAULTS["cot_vivo_keepalive_s"] = 0
    zt.DEFAULTS = dict(zc_config.DEFAULTS, warm_keepalive_s=0,
                       rotar_umbral_mb=0, cot_vivo_keepalive_s=0)
    # F4a: WORKERS/QUESTIONS dejaron de ser globals con path horneado —
    # son accessors (_workers()/_questions()) que resuelven config.STATE_DIR
    # en call-time, así que el rebind del fixture ya no hace falta (la
    # lección del FIX 14/10 quedó absorbida por el patrón F1e de paths).
    os.makedirs(zc_config.LOG_DIR, exist_ok=True)
    return zt


def fresh(testcase):
    """Entorno + módulo nuevos por test (aislamiento total)."""
    import tempfile
    from zcode_bot.commands import dispatch as zc_dispatch
    # F37b: en tests TODO sincrónico — los asserts de los golden leen el
    # VALOR de retorno del handler; el modo async de producción corre el
    # despacho en thread y devuelve el acuse.
    parchea(testcase, zc_dispatch, "SLOW_ASYNC", False)
    root = tempfile.mkdtemp(prefix="zcgolden-")
    env = build_env(root)
    return load_module(env)


def parchea(testcase, target, name, nuevo=mock.DEFAULT, **attrs):
    """Rebind de atributo CON cleanup automático (addCleanup).

    Reemplaza el rebind plano `modulo.nombre = lambda ...`: sin cleanup el
    binding vivía para siempre en el worker de xdist y contaminaba al test
    siguiente (incidente ff53eb5 — `zc_hooks.wait_reply` filtrando entre
    archivos). El target es el módulo dueño del atributo (regla de la
    casa); `nuevo` puede ser un lambda, un objeto, o omitirse para recibir
    un MagicMock configurable por **attrs (side_effect, return_value).
    Devuelve el nuevo valor."""
    p = mock.patch.object(target, name, nuevo, **attrs)
    valor = p.start()
    testcase.addCleanup(p.stop)
    return valor
