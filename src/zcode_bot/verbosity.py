"""verbosity — ONE knob for how much a turn streams to the phone (F61).

Damian, 24/10: «para el celular estaría bueno tener un command que sea  # noqa: spanish — Damian's quoted request
/verbosity que te de 3 opciones summary, normal, y verbose». The SAME  # noqa: spanish — continuation
vocabulary and semantics as the fork's app setting
(messageStreamVerbosity, docs/specs/conversation-verbosity-views.md):
summary = your message + the reply (the middle work stays recoverable
via /cot), normal = today, verbose = the CoT boxes arrive OPEN and every
tool gets its box. One PRESET over the knobs that already exist — the
render pipeline does not learn a new concept, it keeps reading the same
keys; this module is the single place that knows the mapping (and the
WARM refresh below keeps a long-lived worker honest after /verbosity
changes the file).
"""
from typing import Any, Dict

from zcode_bot.utils.config import load_config

LEVELS = ("summary", "normal", "verbose")

# the knobs a preset OWNS (mac_* included: the spectator and the CoT
# watcher of the Mac's turns are the phone's windows too)
KNOBS = ("cot_stream", "cot_buttons", "cot_empty_notice", "tools_stream",
         "tools_box_filter", "live_progress", "mac_spectator",
         "mac_cot_buttons")

# "normal" mirrors config.DEFAULTS verbatim: switching levels is
# deterministic, it never depends on what the file happened to carry.
_LEVEL_KNOBS: Dict[str, Dict[str, Any]] = {
    "summary": {
        "cot_stream": False,        # no text-mode CoT either
        "cot_buttons": False,       # no boxes mid-turn (no cache buttons;
                                    #   /cot still rebuilds from history)
        "cot_empty_notice": False,  # silence is the point of the level
        "tools_stream": False,      # no tool boxes (steps still count)
        "tools_box_filter": "minimal",
        "live_progress": True,      # the ONE live box: proof of life
        "mac_spectator": False,     # Mac turns stream nothing either
        "mac_cot_buttons": False,
    },
    "normal": {
        "cot_stream": True,
        "cot_buttons": True,
        "cot_empty_notice": True,
        "tools_stream": True,
        "tools_box_filter": "action",
        "live_progress": True,
        "mac_spectator": True,
        "mac_cot_buttons": True,
    },
    "verbose": {
        "cot_stream": True,
        "cot_buttons": True,
        "cot_empty_notice": True,
        "tools_stream": True,
        "tools_box_filter": "all",
        "live_progress": True,
        "mac_spectator": True,
        "mac_cot_buttons": True,
    },
}

# one line per level — /verbosity's body and the change confirmations
DESCRIBE = {
    "summary": "your message + the reply; nothing in between "
               "(the CoT stays recoverable with /cot)",
    "normal": "CoT boxes as buttons + boxes for the action tools",
    "verbose": "the CoT boxes arrive OPEN + a box for EVERY tool",
}


def level_of(cfg: Dict[str, Any]) -> str:
    """The level a cfg carries — an unknown value reads as normal (a
    hand-edited config must not mute the chat by accident)."""
    lv = (cfg or {}).get("verbosity")
    return lv if lv in LEVELS else "normal"


def apply_level(cfg: Dict[str, Any], level: str) -> Dict[str, Any]:
    """Sets the level AND its knobs, in place, on a full config dict (the
    poller's cfg IS the config — the handler saves it whole). Raises
    ValueError on an unknown level: the handler owns the friendly error."""
    if level not in LEVELS:
        raise ValueError(level)
    cfg["verbosity"] = level
    cfg.update(_LEVEL_KNOBS[level])
    return cfg


def refresh(cfg: Dict[str, Any], disk: Dict[str, Any] = None) -> None:
    """WARM pickups only: a worker that stays alive between turns keeps
    the cfg it was born with — re-read the level's keys so /verbosity
    applies on the NEXT turn, not on the next process. Transport keys
    (token, chat) are deliberately not touched: the process was built
    around the ones it has."""
    if disk is None:
        disk = load_config()
    for k in ("verbosity",) + KNOBS:
        if k in disk:
            cfg[k] = disk[k]


def born_open(cfg: Dict[str, Any]) -> bool:
    """Verbose freezes its CoT boxes already EXPANDED (the F59 in-place
    expansion as the birth format, ⤴️ to fold back): the app's verbose
    means «reasoning visible and expanded by default» — on the phone the
    equivalent of expanded-by-default is born-open."""
    return level_of(cfg) == "verbose"
