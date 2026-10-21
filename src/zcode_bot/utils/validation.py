"""Validation and parsing utilities for zcode_bot."""
import re
import json
import unicodedata


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


YES_EXACT = {"si", "yes", "y", "ok", "okay", "dale", "1", "acepto", "go", "apru", "apruébalo"}
NO_EXACT = {"no", "n", "nop", "nope", "2", "never", "núnca"}
YES_PREFIX = ("aprob", "allow", "permit", "acept", "autoriz")
NO_PREFIX = ("deny", "deneg", "rechaz", "bloque", "cancel", "nope")


def classify(text):
    """Returns 'yes' | 'no' | 'text'."""
    t = norm(text)
    if not t:
        return "text"
    first = re.sub(r"[!?.,;:¡¿]+$", "", t.split(" ", 1)[0])
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