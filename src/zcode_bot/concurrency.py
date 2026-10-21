"""concurrency — compatibility shim.

The real content lives in zcode_bot.state (AtomicStore) and
zcode_bot.state.registry (WorkersRegistry, QuestionFiles — the golden
rule from the 2026-10-12 deadlock still holds: threading.Lock is NOT
reentrant, one lock acquisition per public method). The gateway
(zc_conc) and the tests (_conc) import through here; the state/
package is the single source since the 15/10 consolidation.
"""
from zcode_bot.state import AtomicStore, QuestionFiles, WorkersRegistry

__all__ = ["AtomicStore", "QuestionFiles", "WorkersRegistry"]
