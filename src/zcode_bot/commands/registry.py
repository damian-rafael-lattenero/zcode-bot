"""commands — CommandRegistry + CommandSpec + parser de argumentos.

Patrón: Command Registry (table-driven dispatch), el clásico de bots y CLIs
(click, flask, telegram-bot menus). Reemplaza el if-chain de 220 líneas y
las 9 repeticiones del parseo de argumentos.

Semántica de matcheo (congelada del monolito original):
- "/comando args…" → matchea (case-insensitive), args van al handler.
- "comando" (sin barra) → matchea SOLO exacto: "stop ahora" como texto
  libre NUNCA dispara el comando (así era el if-chain original).

R6 — CommandSpec: un comando se declara UNA vez (nombres, handler,
descripción de menú, slow) y de la tabla de specs se derivan el REG, los
SLOW_CMDS y el menú de Telegram; el /help queda curado (sus líneas están
agrupadas a mano) pero un test exige que TODO comando visible aparezca en
él. Antes, agregar un comando tocaba 4-6 listas paralelas.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    """La declaración única de un comando del 2_bot. names[0] es el nombre
    canónico (el que va al menú); menu_desc vacío = no va al menú ni se
    exige en el /help (callbacks internos como qgo/qdel, o bootstrap como
    start); slow = corre en thread con acuse (F37b)."""

    names: tuple
    fn: object
    menu_desc: str = ""
    slow: bool = False


def rest_of(text):
    """Argumentos de un mensaje de comando: todo lo que sigue al primer
    espacio, normalizado a strip. Idéntico al patrón que el monolito
    repetía 9 veces inline."""
    s = text.strip()
    return s.split(" ", 1)[1].strip() if " " in s else ""


class CommandRegistry:
    """Tabla nombre-normalizado → handler(cfg, rest)."""

    def __init__(self):
        self._by_name = {}

    def register(self, *names):
        def deco(fn):
            for n in names:
                self._by_name[n.lower().lstrip("/")] = fn
            return fn
        return deco

    def lookup(self, text):
        """Devuelve (handler, rest) o (None, None)."""
        s = text.strip()
        if not s:
            return None, None
        tok = s.split()[0]
        rest = rest_of(text)
        if tok.startswith("/"):
            fn = self._by_name.get(tok[1:].lower())
        else:
            fn = self._by_name.get(tok.lower()) if not rest else None
        return fn, rest

    def dispatch(self, cfg, text):
        fn, rest = self.lookup(text)
        return fn(cfg, rest) if fn else None

    def names(self):
        return sorted(self._by_name)
