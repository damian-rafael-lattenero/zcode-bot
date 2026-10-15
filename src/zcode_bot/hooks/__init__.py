"""Hooks module for zcode_bot."""
from .handler import run_hook
from .lifecycle import on_user_prompt, on_stop, on_session_start, on_tool_failure
from .permissions import on_permission_request, _perm_key, _perm_remember, _perm_replay
from .questions import on_ask_user_question, _q_gemela
from zcode_bot.state.manager import _user_input_handler

__all__ = [
    "run_hook",
    "on_user_prompt",
    "on_stop",
    "on_session_start",
    "on_tool_failure",
    "on_permission_request",
    "on_ask_user_question",
    "_perm_key",
    "_perm_remember",
    "_perm_replay",
    "_q_gemela",
    "_user_input_handler",
]