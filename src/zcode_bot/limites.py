"""limites — constants SHARED across modules (F34, ADR-018).

Three homes, one per class of value: the KNOBS (user tunables from
bot-config.json) live ONLY in config.DEFAULTS; DOMAIN constants
(private to a module: COT_CHUNK, CAP_S...) live in their owning
module; what is SHARED across modules lives HERE, with the why of
each one. The gate (scripts/check_no_duplicates.py) requires a single
definition per name — defining here something that already exists in
another module breaks the suite.
"""

# Telegram accepts 4096 chars per message: ALL chat chunking derives
# from this ceiling; each constant reserves its formatting margin
TELEGRAM_MSG_MAX = 4096
CH_TURNO = 3800        # F32: chunk of the Mac's full turn (header + paging suffix)
PARTES_MAX = 6         # ~22.8k chars of output; more → the last one marks the cut
CH_RESPUESTA = 3900    # worker delivery (_send_long_reply) and cap of send()
CH_CAJA = 3400         # tool box (_tool_box): full command/diff
