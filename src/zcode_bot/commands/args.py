"""R7 — command argument parsing helpers.

What actually repeated (verified site by site): the /qgo//qdel pair
parsed «sid:idx» with the SAME verbatim try/except; four commands
extracted «@sess_…» and the leading number with a hand-rolled
split(" ", 1); and the out-of-range number message lived copy-pasted
with only the command name changed. The three live here; the domain
commands pick their LIST and their action — the common parsing is
not duplicated.
"""


def parse_sid_idx(rest):
    """«sid:idx» → (sid, idx) or None if it does not parse. The /queue
    callbacks (qgo:/qdel:) carry exactly this."""
    try:
        sid, idx = rest.rsplit(":", 1)
        return sid, int(idx)
    except ValueError:
        return None


def head_y_resto(rest):
    """(first token, normalized rest) of a command's arguments: the
    split with strip that task/stop/compact repeated inline."""
    partes = (rest or "").split(" ", 1)
    return partes[0], (partes[1].strip() if len(partes) > 1 else "")


def sid_de(head):
    """«@sess_…» → sid (the vocabulary of the session buttons, which
    identify the conversation without ambiguity); None if it is not
    one."""
    return head[1:] if head.startswith("@ses") else None


def fuera_de_lista(cmd, total):
    """The out-of-range number notice — same text in /task, /stop and
    /compact, changing only the suggested command."""
    return "⚠️ Number out of the list (1 to %d) — send /%s." % (total, cmd)
