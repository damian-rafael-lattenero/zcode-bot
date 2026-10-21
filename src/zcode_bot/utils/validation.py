"""Validation and parsing utilities for zcode_bot."""
import re
import json
import unicodedata


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


# Bilingual (P3b): English words added, every Spanish one kept — both
# languages match forever. Literals are stored ACCENTLESS because norm()
# strips accents from user input (apruebalo, nunca) — accented literals
# never matched.
YES_EXACT = {"si", "yes", "y", "ok", "okay", "yeah", "yep", "approve",  # noqa: spanish — bilingual user input, permanent (P3b)
             "dale", "1", "acepto", "go", "apru", "apruebalo"}
NO_EXACT = {"no", "n", "nop", "nope", "2", "never", "nunca", "deny"}  # noqa: spanish — bilingual user input, permanent (P3b)
YES_PREFIX = ("aprob", "approv", "allow", "permit", "acept", "autoriz")  # noqa: spanish — bilingual user input, permanent (P3b)
NO_PREFIX = ("deny", "deneg", "rechaz", "bloque", "cancel", "nope")  # noqa: spanish — bilingual user input, permanent (P3b)


def classify(text):
    """Returns 'yes' | 'no' | 'text'."""
    t = norm(text)
    if not t:
        return "text"
    first = re.sub(r"[!?.,;:¡¿]+$", "", t.split(" ", 1)[0])  # noqa: spanish — Spanish opening punctuation is genuine user input
    if first in YES_EXACT or t in YES_EXACT:
        return "yes"
    if first in NO_EXACT or t in NO_EXACT:
        return "no"
    if any(first.startswith(p) for p in YES_PREFIX):
        return "yes"
    if any(first.startswith(p) for p in NO_PREFIX):
        return "no"
    return "text"


def first(payload, *keys):
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return None