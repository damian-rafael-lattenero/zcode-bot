"""R5 — the button callback vocabulary: ONE table.

Every inline button the bot sends carries a short callback_data (≤64 B,
Telegram's limit). Its translation to "what the owner typed" lived in a
26-branch if-chain (updates._cb_text) while the ~20 prefix PRODUCERS
were scattered across reporting/handlers/workers/hooks/razonamiento —
split knowledge, an implicit contract. The single table lives here:
the styles cover the four real shapes (raw argument, argument with
«:»→space, session with @, free text) and the exact ones without an
argument.

Compatibility notes the table PRESERVES on purpose:
- t:ses / s:ses / c:ses match only with «ses…»: a weird «t:x» falls to
  the default (free text), as always.
- task:N is the OLD vocabulary (position in the full list, ambiguous
  across workspaces) — old buttons still live in the owner's chats.
- «planfix» (hooks: «✏️ Agregar algo al plan») is NOT mapped: it falls
  to the default and travels as free text — which, in a pending
  permission context, arrives as the answer to the agent. That is
  today's behavior, made explicit here so it stops being a silent
  accident.
"""
import re

# argument styles
_AR = "ar"        # "/cmd " + rest (the «:» stay: sid:idx)
_ESP = "esp"      # "/cmd " + rest with «:» → « »
_SES = "ses"      # "/cmd @" + rest (identifies the conversation)
_LIBRE = "libre"  # the rest as-is: NOT a command, it is the owner's text

# EXACT tokens (no argument)
EXACTOS = {
    "retry": "/retry",
    "yes": "yes",              # bilingual forever: old buttons live 24h
    "si": "si",                # in chats (R5 + P3b)
    "no": "no",
    "nuc:go": "/nuclear go",   # ☢️ confirmation (two steps)
    "nuc:no": "/nuclear no",
    "dep:go": "/deploy go",    # 🚀 confirmation (two steps)
    "dep:no": "/deploy no",
    "dl": "/diff log",         # F38e: history
    "dw": "/diff",             # F38e: work in progress
}

# prefixes with argument: prefix → (command, style)
PREFIJOS = {
    "t:ses": ("/task", _SES),       # t:sess_... (and old ses_)
    "s:ses": ("/stop", _SES),
    "c:ses": ("/compact", _SES),
    "mo:": ("/mode", _AR),          # Plan mode / Full access
    "qgo:": ("/qgo", _AR),          # F22: launch a queued item NOW (sid:idx)
    "qdel:": ("/qdel", _AR),        # F22: discard a queued item
    "m:": ("/model", _AR),
    "th:": ("/thinking", _AR),
    "task:": ("/all_tasks", _AR),   # old vocabulary (position)
    "ans:": ("", _LIBRE),           # textual answer to a question
    "df:": ("/diff", _ESP),         # F38b: file n [+hash +part]
    "dx:": ("/diff dirty", _ESP),   # F38b: uncommitted changes
    "dc:": ("/diff commit", _AR),   # F38e: a commit from the list
    "ct:": ("/cotb", _ESP),         # F40: box n [part p] of the cached CoT
}

# longest prefixes first: if tomorrow there is «t:» and «t:ses», the
# specific one wins (no collision today; the order makes it future-proof)
_PREFIJOS_ORD = sorted(PREFIJOS.items(), key=lambda kv: -len(kv[0]))


def texto_de_callback(data):
    """callback_data → the text the owner «typed». Default: the first
    64 chars as free text (historical behavior)."""
    if data in EXACTOS:
        return EXACTOS[data]
    for pref, (cmd, estilo) in _PREFIJOS_ORD:
        if data.startswith(pref):
            # _SES matches «ses…» but consumes ONLY the 2 letters of the
            # real prefix (t:/s:/c:) — the session goes WHOLE after the @,
            # same as the historical if-chain
            resto = data[2:] if estilo == _SES else data[len(pref):]
            if estilo == _ESP:
                return "%s %s" % (cmd, resto.replace(":", " "))
            if estilo == _SES:
                return "%s @%s" % (cmd, resto)
            if estilo == _LIBRE:
                return resto
            return "%s %s" % (cmd, resto)
    return data[:64]


def es_respuesta_de_pregunta(data):
    """R5 — does the callback answer a pending agent question/permission?
    The stale-question guard (previously inline in gateway) reads this:
    without a pending question the button is NOT injected into the
    active conversation."""
    return bool(data) and (data.startswith("ans:")
                           or data in ("yes", "si", "no"))


def callback_data_valido(data):
    """Telegram's ≤64 BYTES (real 41-char sid + t0 + n + p = 61 in the
    real worst case — the F40 test guarantees it by construction)."""
    return bool(data) and len(data.encode()) <= 64 and not re.search(
        r"[\s\n]", data)
