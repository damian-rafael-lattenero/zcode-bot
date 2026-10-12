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
import time

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "zcode-telegram.py")

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
    """Carga zcode-telegram.py con todos los paths apuntando al fixture."""
    spec = importlib.util.spec_from_file_location("zt_golden", SRC)
    zt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(zt)
    zt.BASE = env["base"]
    zt.STATE_DIR = env["state"]
    zt.LOG_DIR = os.path.join(env["base"], "hooks", "logs")
    zt.CONFIG_PATH = os.path.join(env["base"], "telegram-remote.json")
    zt.DEFAULT_WS = DEFAULT_WS
    os.makedirs(zt.LOG_DIR, exist_ok=True)
    return zt


def fresh(testcase):
    """Entorno + módulo nuevos por test (aislamiento total)."""
    import tempfile
    root = tempfile.mkdtemp(prefix="zcgolden-")
    env = build_env(root)
    return load_module(env)
