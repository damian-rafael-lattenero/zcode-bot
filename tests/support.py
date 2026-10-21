"""Test support: deterministic FAKE ~/.zcode environment + module loading
with patched paths. No test touches the real ~/.zcode or the network.

Pattern: Test Double (fixture environment) — the module is loaded via
importlib and its path constants (BASE/STATE_DIR/...) are patched, because
the monolith resolves paths at call-time from those globals.
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

# src/ goes on sys.path WHEN support IS IMPORTED (not in load_module):
# the import order across test files used to be non-deterministic
# (exposed by the parallel runner: each file in its own process)
_src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# F34d: REAL constants from src — a test that needs a limit imports it
# from here instead of re-hardcoding it (when src changes, the test
# still describes the same contract)
from zcode_bot.limites import (CH_CAJA, CH_RESPUESTA, CH_TURNO, PARTES_MAX,  # noqa: E402
                               TELEGRAM_MSG_MAX)
from zcode_bot.audit import CAP as AUDIT_CAP  # noqa: E402
FAKE_PID = 424242  # canonical fake pid for auditing/orphans


def make_fake_tg_api(cap):
    """Canonical tg_api mock: records params per method into the given
    dict (test_f3, test_f6_nuclear and test_f7_trace duplicated this
    signature)."""
    def fake_tg_api(cfg, method, params=None, timeout=35):
        cap[method] = params
    return fake_tg_api


# Deterministic fixture data (dates relative to "now" only where the
# code compares them against time.time(); texts are fixed)
PROJ_BOT = "/fake/proj/bot"
PROJ_CV = "/fake/proj/cv"
PROJ_HS = "/fake/proj/haskell"
DEFAULT_WS = "/fake/.zcode/workspace/default"

T1 = "sess_TEST00000000000000000000001"  # testing (bot) — running
T2 = "ses_TEST000000000000000000000002"  # CV — old sid format (ses_)
T3 = "sess_TEST00000000000000000000003"  # MCP
T4 = "sess_TEST00000000000000000000004"  # instalaciones (loose, default ws)


def _iso(epoch_s):
    import datetime
    return datetime.datetime.fromtimestamp(epoch_s, datetime.timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_env(root):
    """Creates the fake tree and returns a dict with the key paths."""
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

    # --- CLI logs (turn.* and compact.* events)
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
        f.write("Sample last reply for /last.")
    return env


def load_module(env):
    """Loads the gateway and rebinds zcode_bot.config to the fixture.

    Paths are ALWAYS read as an attribute (config.STATE_DIR) — a single
    rebind point for gateway and utils (F1e: before, the gateway read
    the fixture while utils read production — a silent divergence)."""
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
    # F8: hot-worker/rotation knobs AT 0 for the suite — the production
    # default (600s/8MB) would make the warm loop wait in every
    # prompt_worker test. A test that wants them passes them in its cfg.
    # F40: the --cot-botones spawn in on_stop is gated by cfg; load_config
    # reads DEFAULTS by the real module's name (not this zt copy): it is
    # turned off IN THE REAL DICT so no test spawns subprocesses. A test
    # that wants it turns it on in its cfg.
    zc_config.DEFAULTS["mac_cot_botones"] = False
    zc_config.DEFAULTS["cot_botones"] = False  # F42: likewise, button box
    # F44: the hot watchdog would sleep up to 600s waiting for the next
    # marker — in the suite, one turn and done (a test that wants the wait
    # passes an explicit keepalive with fake clock/sleep)
    zc_config.DEFAULTS["cot_vivo_keepalive_s"] = 0
    zt.DEFAULTS = dict(zc_config.DEFAULTS, warm_keepalive_s=0,
                       rotar_umbral_mb=0, cot_vivo_keepalive_s=0)
    # F4a: WORKERS/QUESTIONS are no longer globals with a baked-in path —
    # they are accessors (_workers()/_questions()) that resolve
    # config.STATE_DIR at call-time, so rebinding the fixture is no longer
    # needed (the lesson from FIX 14/10 was absorbed by the F1e path
    # pattern).
    os.makedirs(zc_config.LOG_DIR, exist_ok=True)
    return zt


def fresh(testcase):
    """Fresh environment + module per test (total isolation)."""
    import tempfile
    from zcode_bot.commands import dispatch as zc_dispatch
    # F37b: in tests everything is synchronous — the golden asserts read
    # the handler's return VALUE; the production async mode runs the
    # dispatch in a thread and returns the acknowledgement.
    patch(testcase, zc_dispatch, "SLOW_ASYNC", False)
    root = tempfile.mkdtemp(prefix="zcgolden-")
    env = build_env(root)
    return load_module(env)


def patch(testcase, target, name, nuevo=mock.DEFAULT, **attrs):
    """Attribute rebind WITH automatic cleanup (addCleanup).

    Replaces the plain rebind `module.attr = lambda ...`: without cleanup
    the binding lived forever in the xdist worker and contaminated the
    next test (incident ff53eb5 — `zc_hooks.wait_reply` leaking across
    files). The target is the module owning the attribute (house rule);
    `nuevo` can be a lambda, an object, or omitted to receive a MagicMock
    configurable via **attrs (side_effect, return_value).
    Returns the new value."""
    p = mock.patch.object(target, name, nuevo, **attrs)
    valor = p.start()
    testcase.addCleanup(p.stop)
    return valor


class FakeClock:
    """R12 — fake clock for timing tests: time() advances `step_s` per
    call (the dict-clock pattern that test_f42 duplicated inline twice).
    sleep(s) also advances by default (`with_sleep=False` for when the
    code under test sleeps its own pacing and the test wants exact
    steps)."""

    def __init__(self, t0=1760000000.0, step_s=10.0, with_sleep=True):
        self.t = float(t0)
        self.paso = float(step_s)
        self.with_sleep = with_sleep

    def time(self):
        self.t += self.paso
        return self.t

    def sleep(self, s):
        if self.with_sleep:
            self.t += float(s)


def wait_until(cond, timeout=5.0, paso=0.02):
    """R12 — synchronous wait-until for tests: sleeps `paso` until
    cond() is truthy or the ceiling expires (the inline while+time.sleep
    that cmds_deploy and f38 duplicated). Returns True/False; the caller
    makes the assert, with ITS own message."""
    fin = time.time() + timeout
    while time.time() < fin:
        if cond():
            return True
        time.sleep(paso)
    return bool(cond())
