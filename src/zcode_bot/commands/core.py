"""commands.core — comandos básicos: start, help, ping, status."""
import asyncio

from zcode_bot.utils.config import load_config
from zcode_bot.utils.formatting import esc, preview
from zcode_bot.utils.http import send, send_document
from zcode_bot.utils.logging import log, pwlog

from zcode_bot import proto as zc_proto
from zcode_bot import fswatch

from zcode_bot.state import WORKERS, QUESTIONS, _task_current_ok
from zcode_bot.state.manager import _active_turn_register, _active_turn_clear, _runtime_muerto, _TurnAbandoned, _orphan_fetch_text, _orphans_check, _q_gemela, _user_input_handler
from zcode_bot.state.registry import WorkersRegistry, QuestionFiles


def cmd_start(cfg, rest):
    return "🤖 Bot de control de ZCode activo.\nComandos: /quota — /ping — /help"


def cmd_help(cfg, rest):
    return ("🤖 <b>Bot unificado ZCode</b> (headless — sirve con la Mac bloqueada)\n"
            "<b>Conversaciones</b>\n"
            "/task — con Project · /task n elige · /task new [proyecto]\n"
            "/all_tasks — todas, incluidas las sueltas del grupo Tasks\n"
            "texto libre — corre en la conversación activa\n"
            "/run [proyecto] prompt — nueva conversación + prompt\n"
            "/model — modelo de la activa · /thinking — max/high/low\n"
            "/mode — Plan mode 🗺 · Full access 🔓 (botones)\n"
            "/fork — bifurcar la activa · /last — última respuesta completa\n"
            "/cot — paso a paso + CoT completo del turno (documento)\n"
            "<b>Control</b>\n"
            "/quota — credits 5h + semanal del plan\n"
            "/usage — detalle de hoy por sesión\n"
            "/ctx — tasks con % de contexto\n"
            "/ping — prueba de vida\n"
            "/status — workers (pid), colas, config de la activa, salud\n"
            "/queue — contenido de las colas (⏫ lanzar / 🗑 descartar)\n"
            "/retry — relanza el último prompt (tras fallo o crash)\n"
            "/stop — frena tu corrida del celular y LIMPIA su cola de mensajes\n"
            "/nuclear — ☢️ emergencia: frena TODO (celular + Mac) y reinicia\n"
            "/deploy — 🚀 corre el pipeline (tests→backup→recarga) con botón\n"
            "/compact — lista Projects para compactar · /all_compact todas\n"
            "(permisos 🔐 y preguntas ❓ se responden tocando los botones, o con texto/número)")


def cmd_ping(cfg, rest):
    return "🏓 pong"


def cmd_status(cfg, rest):
    from zcode_bot.gateway import status_report
    return status_report()