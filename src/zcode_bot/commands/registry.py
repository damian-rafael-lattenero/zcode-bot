"""commands — CommandRegistry + CommandSpec + argument parsing.

Pattern: Command Registry (table-driven dispatch), the classic for bots
and CLIs (click, flask, telegram-bot menus). Replaces the 220-line
if-chain and the 9 repetitions of argument parsing.

Matching semantics (frozen from the original monolith):
- "/command args…" → matches (case-insensitive), args go to the handler.
- "command" (no slash) → matches ONLY exactly: "stop now" as free text
  NEVER triggers the command (that is how the original if-chain
  behaved).

R6 — CommandSpec: a command is declared ONCE (names, handler, menu
description, slow) and the REG, SLOW_CMDS and the Telegram menu are
derived from the spec table; /help stays curated (its lines are grouped
by hand) but a test requires that EVERY visible command appear in it.
Before, adding a command touched 4-6 parallel lists.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    """The single declaration of a 2_bot command. names[0] is the
    canonical name (the one that goes to the menu); empty menu_desc =
    not in the menu nor required in /help (internal callbacks like
    qgo/qdel, or bootstrap like start); slow = runs in a thread with
    an ack (F37b)."""

    names: tuple
    fn: object
    menu_desc: str = ""
    slow: bool = False


def rest_of(text):
    """Arguments of a command message: everything after the first space,
    normalized with strip. Identical to the pattern the monolith
    repeated 9 times inline."""
    s = text.strip()
    return s.split(" ", 1)[1].strip() if " " in s else ""


class CommandRegistry:
    """Table normalized-name → handler(cfg, rest)."""

    def __init__(self):
        self._by_name = {}

    def register(self, *names):
        def deco(fn):
            for n in names:
                self._by_name[n.lower().lstrip("/")] = fn
            return fn
        return deco

    def lookup(self, text):
        """Returns (handler, rest) or (None, None)."""
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
