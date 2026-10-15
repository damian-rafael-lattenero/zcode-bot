"""Commands package for zcode_bot."""
from .registry import CommandRegistry, rest_of
from .core import cmd_start, cmd_help, cmd_ping, cmd_status
from .tasks import cmd_task, cmd_all_tasks, cmd_run
from .model import cmd_model, cmd_thinking, cmd_mode
from .control import cmd_stop, cmd_nuclear, cmd_deploy, cmd_retry
from .queues import cmd_queue, cmd_qgo, cmd_qdel
from .context import cmd_compact, cmd_all_compact, cmd_cot
from .webhook import cmd_set_webhook, cmd_delete_webhook, cmd_webhook_info
from .context import cmd_compact, cmd_all_compact, cmd_cot

__all__ = [
    "CommandRegistry",
    "rest_of",
    "cmd_start",
    "cmd_help",
    "cmd_ping",
    "cmd_status",
    "cmd_task",
    "cmd_all_tasks",
    "cmd_run",
    "cmd_model",
    "cmd_thinking",
    "cmd_mode",
    "cmd_stop",
    "cmd_nuclear",
    "cmd_deploy",
    "cmd_retry",
    "cmd_queue",
    "cmd_qgo",
    "cmd_qdel",
    "cmd_compact",
    "cmd_all_compact",
    "cmd_cot",
    "cmd_set_webhook",
    "cmd_delete_webhook",
    "cmd_webhook_info",
]