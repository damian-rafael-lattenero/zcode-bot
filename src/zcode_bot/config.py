"""config — THE single source of the bot's paths and configuration (F1e).

Every module reads paths AS AN ATTRIBUTE (config.STATE_DIR) at call-time:
this way the test fixture rebinds ONCE here and the gateway-vs-utils
divergence disappears (the historical bug: the gateway read the fixture
while utils read production). Nothing should do `from config import
STATE_DIR` (that copies the value at import time — that copy IS the
divergence).
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


# --- F34b: shared-state file names (one source) ---
# The pattern is `_store(config.QUEUE_FILE)` / `os.path.join(config.STATE_DIR,
# config.QUEUE_FILE)`: a rename today means touching THIS list, not hunting strings.
WORKERS_FILE = "bot-workers.json"
QUEUE_FILE = "bot-queue.json"
SESSION_FILE = "bot-session.json"
AUDIT_FILE = "bot-auditoria.json"
LIVE_SKIP_FILE = "bot-live-skip.json"
RELOAD_FILE = "reload-now"
LAST_REPLY_FILE = "last-reply.txt"
LAST_TRACE_FILE = "last-turn-trace.txt"

# R0b/F51c: how often to send the «⏱️ lleva N min» notice on a long turn.
# The worker (phone turns, workers._run_turn via live_view) and the
# Mac watcher (razonamiento._watch_turn) read the SAME value: the
# silence of a stretching turn read as «se tildó» (Damian, night of
# 20/10 and morning of 21/10).
LONG_TURN_NOTICE_S = 1800


def marker_path(session_id):
    """Marker for the in-flight turn (the definition used to live in
    hooks; moved here so the name has ONE owner — gateway and espectador
    import it all the same)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "ses")
    return os.path.join(STATE_DIR, "turn-%s.marker" % safe)


def stop_dir():
    """Directory of /stop markers."""
    return os.path.join(STATE_DIR, "bot-stop")


def rollout_path(sid):
    """Model-io jsonl of a session (F31 /ctx, F33 espectador)."""
    return os.path.join(BASE, "cli", "rollout", "model-io-%s.jsonl" % sid)


def code_stamp():
    """F48d — stamp of the installed code: the package's mtime (deploy.sh
    replaces it wholesale, rm -rf + cp → it changes). HOT processes (warm
    worker, Mac watcher) compare it against the one from their startup and
    bow out when they see it change, so the next turn starts cold with the
    new code (before: up to 10 min of new poller + old worker per deploy,
    20/10 20:37-20:56). None if the package is absent (the window between
    the rm and the cp): counts as a change."""
    try:
        return os.path.getmtime(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "__init__.py"))
    except OSError:
        return None


CODE_STAMP_START = code_stamp()


def code_changed():
    """Was there a deploy since this process started?"""
    return code_stamp() != CODE_STAMP_START

DEFAULTS = {
    "token": "",
    "chat_id": None,
    "owner_user_id": None,
    "owner_name": "",
    # F52 (Damian, 21/10): phone buttons wait 24 h — a plan/question
    # permission visible on the phone doesn't expire after 4 min (or 60 s
    # for questions) when you're away. The busy-flag window does NOT
    # derive from this (see updates.BUSY_FRESH_S): a dead hook must not
    # leave the bot mute for a whole day.
    "wait_timeout_s": 86400,
    "question_wait_timeout_s": 86400,
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
    # F8 (ADR-014): warm worker — the runtime waits for the session's next
    # prompt for these seconds (0 = one process per turn, as always). And
    # rotation: transcript size (MB) that triggers compact+summary+a
    # seeded new session (0 = off). See workers._warm_wait/_rotate_session.
    "warm_keepalive_s": 600,
    "rotar_umbral_mb": 8,
    # F9: days to keep the transcript (rollout model-io jsonl) of sessions
    # the bot no longer references (rotated/archived) — the file is left
    # behind on each rotation and grows ~one transcript at a time (44MB
    # measured). 0 = off.
    "rollout_retain_dias": 7,
    # F30: invariant auditor — pass cadence (0 = off), failures of the
    # same type+session within 10 min that trigger the "sick session"
    # alert, and cooldown minutes per alert (anti-spam).
    "auditor_cada_s": 30,
    "auditor_fallos_min": 2,
    "auditor_alerta_cooldown_min": 60,
    # F33: espectador of the Mac's turn (rollout tail → Telegram);
    # turning it off here kills the whole feature (cot_stream/
    # tools_stream remain on)
    "mac_espectador": True,
    "mac_cot_botones": True,  # F40: when a Mac turn closes, CoT n buttons with their reasoning
    # F44: the --cot-vivo watcher stays HOT waiting for the next prompt of
    # the same Mac session (one resume per session, not per turn — the
    # first little box goes from ~48s to seconds). 0 = one watcher per turn.
    "cot_vivo_keepalive_s": 600,
    # F7 turn streaming: CoT in boxes and tool boxes (false = silent)
    "cot_stream": True,

    "cot_botones": True,  # F42: the phone's CoT as buttons that grow per phase (replaces cot_stream's text)
    # F45: a turn that closes WITHOUT reasoning says so in one line (the
    # three sources: phone, live Mac, capture at close). GLM-5.3 reasons
    # adaptively and with long context often skips it — without the
    # notice, zero boxes was indistinguishable from a bug.
    "cot_aviso_vacio": True,
    "tools_stream": True,
    "tools_caja_filtro": "accion",  # todas | accion | minimo (messages)
    "live_progress": True,          # live progress box (F7)
    "trace_documents": False,       # /cot: save the turn's document
    "headless_guard": True,         # anti-suicide guardrail (F4d)
    # ADR-011: webhook endpoint (written by /setwebhook)
    "webhook_url": "",
    "webhook_secret": "",
    # ADR-011: updates transport, mutually exclusive
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
    # tmp+replace (deep audit 20/10): a crash mid-json.dump left the file
    # truncated → load_config silently fell back to DEFAULTS (empty
    # token) → deaf bot with NO warning at all
    tmp = "%s.tmp.%d" % (CONFIG_PATH, os.getpid())
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def configured(cfg):
    return bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))
