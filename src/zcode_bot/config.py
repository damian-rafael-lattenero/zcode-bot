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
from typing import Any, Dict, Optional

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
AUDIT_FILE = "bot-audit.json"
AUDIT_FILE_LEGACY = "bot-auditoria.json"  # P6: read/purged until it ages out
LIVE_SKIP_FILE = "bot-live-skip.json"
FORCE_STOP_FILE = "bot-forcestop.json"  # F58: tombstones of force-killed turns
RELOAD_FILE = "reload-now"
LAST_REPLY_FILE = "last-reply.txt"
LAST_TRACE_FILE = "last-turn-trace.txt"

# R0b/F51c: how often to send the «⏱️ lleva N min» notice on a long turn.
# The worker (phone turns, workers._run_turn via live_view) and the
# Mac watcher (reasoning._watch_turn) read the SAME value: the
# silence of a stretching turn read as «se tildó» (Damian, night of  # noqa: spanish — Damian's quoted remark (20/10 incident)
# 20/10 and morning of 21/10).
LONG_TURN_NOTICE_S = 1800


def marker_path(session_id: Optional[str]) -> str:
    """Marker for the in-flight turn (the definition used to live in
    hooks; moved here so the name has ONE owner — gateway and spectator
    import it all the same)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "ses")
    return os.path.join(STATE_DIR, "turn-%s.marker" % safe)


def stop_dir() -> str:
    """Directory of /stop markers."""
    return os.path.join(STATE_DIR, "bot-stop")


def rollout_path(sid: str) -> str:
    """Model-io jsonl of a session (F31 /ctx, F33 spectator)."""
    return os.path.join(BASE, "cli", "rollout", "model-io-%s.jsonl" % sid)


def code_stamp() -> Optional[float]:
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


def code_changed() -> bool:
    """Was there a deploy since this process started?"""
    return code_stamp() != CODE_STAMP_START

DEFAULTS: Dict[str, Any] = {
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
    "rotate_threshold_mb": 8,
    # F9: days to keep the transcript (rollout model-io jsonl) of sessions
    # the bot no longer references (rotated/archived) — the file is left
    # behind on each rotation and grows ~one transcript at a time (44MB
    # measured). 0 = off.
    "rollout_retain_days": 7,
    # F30: invariant auditor — pass cadence (0 = off), failures of the
    # same type+session within 10 min that trigger the "sick session"
    # alert, and cooldown minutes per alert (anti-spam).
    "auditor_interval_s": 30,
    "auditor_failures_min": 2,
    "auditor_alert_cooldown_min": 60,
    # F33: spectator of the Mac's turn (rollout tail → Telegram);
    # turning it off here kills the whole feature (cot_stream/
    # tools_stream remain on)
    "mac_spectator": True,
    "mac_cot_buttons": True,  # F40: when a Mac turn closes, CoT n buttons with their reasoning
    # F44: the --cot-live watcher stays HOT waiting for the next prompt of
    # the same Mac session (one resume per session, not per turn — the
    # first little box goes from ~48s to seconds). 0 = one watcher per turn.
    "cot_live_keepalive_s": 600,
    # F7 turn streaming: CoT in boxes and tool boxes (false = silent)
    "cot_stream": True,

    "cot_buttons": True,  # F42: the phone's CoT as buttons that grow per phase (replaces cot_stream's text)
    # F45: a turn that closes WITHOUT reasoning says so in one line (the
    # three sources: phone, live Mac, capture at close). GLM-5.3 reasons
    # adaptively and with long context often skips it — without the
    # notice, zero boxes was indistinguishable from a bug.
    "cot_empty_notice": True,
    "tools_stream": True,
    "tools_box_filter": "action",  # all | action (default) | minimal (messages)
    "live_progress": True,          # live progress box (F7)
    # F61: ONE level for how much a turn streams (summary | normal |
    # verbose) — verbosity.py owns the preset over the knobs above
    "verbosity": "normal",
    "trace_documents": False,       # /cot: save the turn's document
    "headless_guard": True,         # anti-suicide guardrail (F4d)
    # ADR-011: webhook endpoint (written by /setwebhook)
    "webhook_url": "",
    "webhook_secret": "",
    # ADR-011: updates transport, mutually exclusive
    "transport": "polling",
}


# F53: English knob names — a config file written by an older build keeps
# working: an old key only wins when the new one is absent from the file.
LEGACY_KNOBS = {
    "rotar_umbral_mb": "rotate_threshold_mb",
    "rollout_retain_dias": "rollout_retain_days",
    "auditor_cada_s": "auditor_interval_s",
    "auditor_fallos_min": "auditor_failures_min",
    "auditor_alerta_cooldown_min": "auditor_alert_cooldown_min",
    "mac_espectador": "mac_spectator",
    "mac_cot_botones": "mac_cot_buttons",
    "cot_vivo_keepalive_s": "cot_live_keepalive_s",
    "cot_aviso_vacio": "cot_empty_notice",
    "cot_botones": "cot_buttons",
    "tools_caja_filtro": "tools_box_filter",
}
_TOOL_FILTER_VALUES = {"todas": "all", "accion": "action", "minimo": "minimal"}  # noqa: spanish — legacy config values, permanent read compat


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            on_disk = json.load(f)
    except (OSError, ValueError):
        on_disk = {}
    for old, new in LEGACY_KNOBS.items():
        if old in on_disk and new not in on_disk:
            on_disk[new] = on_disk[old]
    cfg.update(on_disk)
    # legacy Spanish values of the tools filter normalize to English
    cfg["tools_box_filter"] = _TOOL_FILTER_VALUES.get(
        cfg.get("tools_box_filter"), cfg.get("tools_box_filter"))
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    # tmp+replace (deep audit 20/10): a crash mid-json.dump left the file
    # truncated → load_config silently fell back to DEFAULTS (empty
    # token) → deaf bot with NO warning at all
    tmp = "%s.tmp.%d" % (CONFIG_PATH, os.getpid())
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def configured(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))
