"""commands.webhook — comandos de webhook: /set_webhook, /delete_webhook, /webhook_info."""
from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

import os
import json
import sys
import time
import subprocess
import threading
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send

from zcode_bot import proto as zc_proto

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok, active_bot_session, set_active_bot_session, running_turns
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles

def _cfg(zt, **extra):
    cfg = dict(zt.DEFAULTS)
    cfg.update(token="T", chat_id=1, owner_user_id=9, enabled=True)
    cfg.update(extra)
    return cfg


def cmd_set_webhook(cfg, rest):
    """/set_webhook <URL> [secret] — configura webhook de Telegram."""
    if not rest:
        return "⚠️ Uso: /set_webhook <URL> [secret_token]"
    partes = rest.split(" ", 1)
    url = partes[0].strip()
    secret = partes[1].strip() if len(partes) > 1 else None
    
    if not url.startswith("https://"):
        return "⚠️ La URL debe ser HTTPS"
    
    ok = asyncio.run(zc_webhook.set_webhook(cfg, url, secret))
    if ok:
        cfg["webhook_url"] = url
        if secret:
            cfg["webhook_secret"] = secret
        save_config(cfg)
        return f"✅ Webhook configurado: {url}"
    else:
        return "❌ Falló configuración de webhook"


def cmd_delete_webhook(cfg, rest):
    """/delete_webhook — elimina webhook y vuelve a long-polling."""
    ok = asyncio.run(zc_webhook.delete_webhook(cfg))
    if ok:
        cfg.pop("webhook_url", None)
        cfg.pop("webhook_secret", None)
        save_config(cfg)
        return "✅ Webhook eliminado (vuelve a long-polling)"
    else:
        return "❌ Falló eliminación de webhook"


def cmd_webhook_info(cfg, rest):
    """/webhook_info — muestra info del webhook actual."""
    info = asyncio.run(zc_webhook.get_webhook_info(cfg))
    return json.dumps(info, indent=2, ensure_ascii=False)
