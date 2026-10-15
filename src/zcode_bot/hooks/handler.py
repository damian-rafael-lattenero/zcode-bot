"""hooks.handler — dispatcher de hooks de ZCode."""
import json
import sys

from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import log, log_payload


def run_hook():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"_raw": raw[:2000]}
    cfg = load_config()
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or
                os.environ.get("ZCODE_HOOK_EVENT", "unknown"))
    log_payload(event, payload)
    if event == "PreToolUse":
        name, _ = tool_desc(payload)
        if str(name) == "AskUserQuestion":
            on_ask_user_question(cfg, payload)
        return
    handler = {
        "UserPromptSubmit": on_user_prompt,
        "Stop": on_stop,
        "PermissionRequest": on_permission_request,
        "PostToolUseFailure": on_tool_failure,
        "SessionStart": on_session_start,
    }.get(event)
    if handler:
        handler(cfg, payload)


import os
from zcode_bot import state as zc_state
from zcode_bot import concurrency as zc_conc
from zcode_bot import proto as zc_proto
from zcode_bot import cli_logs as zc_logs
from zcode_bot import tasks_repo as zc_repo
from zcode_bot import fswatch
from zcode_bot import commands as zc_commands
from zcode_bot import reports as zc_reports
from zcode_bot import webhook_server as zc_webhook

from zcode_bot.hooks.lifecycle import on_user_prompt, on_stop, on_session_start, on_tool_failure
from zcode_bot.hooks.permissions import on_permission_request
from zcode_bot.hooks.questions import on_ask_user_question
from zcode_bot.hooks.permissions import tool_desc