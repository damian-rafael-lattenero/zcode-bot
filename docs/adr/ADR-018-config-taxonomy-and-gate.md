# ADR-018 — Configuration taxonomy: three homes + gate

**Status:** accepted (2026-10-19). Context: "a thousand magic numbers
everywhere" [«mil magic numbers por todos lados»] (Damian) — F34
inventory: ~565 literals in the 15 worst src files, the live Telegram
message size as 6 different numbers, 3 knob-fallback styles, 7 knobs
that existed only as inline fallbacks, state paths hand-written in ~20
places and 24 references to loose file names.

## Decision — three homes, one per class of value

1. **KNOB** (user-tunable from bot-config.json) → lives ONLY in
   `config.DEFAULTS`, with a comment. The code reads it with
   `cfg.get("key")` and NO fallback (load_config already merges
   DEFAULTS). A residual `or` guard is protection against an explicit
   JSON `null`, not duplication. F34c fixed 2 real divergences:
   `loud_all` (utils/http did `.get("loud_all", False)` — it inverted
   the sound outside load_config) and `suppress_error_patterns` (the
   hooks lost the 5 DEFAULTS regexes with their `[]` fallback).
2. **DOMAIN CONSTANT** (private to a module: COT_CHUNK, CAP_S,
   LOG_TAIL...) → commented UPPER_CASE in its owning module. It is not
   centralized: the catch-all mega-file rots.
3. **SHARED CONSTANT** (across modules) → `limits.py` (then
   `limites.py`), small and with the why: TELEGRAM_MSG_MAX=4096 +
   derivatives (CH_TURN 3800, CH_REPLY 3900, CH_BOX 3400, PARTES_MAX
   6).
4. **PATHS** → file names in config (`QUEUE_FILE`, `SESSION_FILE`... —
   then `ARCH_COLA`, `ARCH_SESION`) + constructors (`marker_path`,
   `stop_dir`, `rollout_path`) — a rename today means touching ONE
   list.

## Enforcement — scripts/check_no_duplicates.py (F34e)

F5's gate grew three AST checks:
1. **Duplicates** (the original): one symbol, one body.
2. **Module attributes**: every referenced UPPER `config.X` /
   `limits.X` (then `lim.X`) must exist in the owner — born from the
   10/19 incident: a missing `ARCH_RELOAD` (today `RELOAD_FILE`) left
   the poller deaf for 5 minutes IN DEADLOCK (the reload mechanism read
   the same missing attribute; neither the suite nor the import gate
   caught it because it is an attribute, not an import).
3. **Magic numbers**: keyword `timeout=`/similar, slices ≥1000 and
   `cfg.get` with a literal fallback are a violation except for
   ALLOW_MAGIC (the audited preexisting ones; anything new gets named
   or justified).

## Consequences

- Changing a limit/time means touching a name, and the suite describes
  the same contract (the tests import the real constants via support).
- The gate's allowlist is explicit and bounded: growing it requires a
  line with justification — going back to the thousand numbers is not
  free.
- Out of scope: dataclass/pydantic for config (the dict stays), trivial
  literals (indices, log trims with no semantics), and a shared fake
  clock for timing tests (parked).
