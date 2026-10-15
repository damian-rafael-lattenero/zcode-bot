"""concurrency — shim de compatibilidad.

El contenido real vive en zcode_bot.state (AtomicStore) y
zcode_bot.state.registry (WorkersRegistry, QuestionFiles — la regla de
oro del deadlock del 2026-10-12 sigue: threading.Lock NO es reentrante,
un lock por método público). El gateway (zc_conc) y los tests (_conc)
importan por acá; el paquete state/ es la fuente única desde la
consolidación del 15/10.
"""
from zcode_bot.state import AtomicStore, QuestionFiles, WorkersRegistry

__all__ = ["AtomicStore", "QuestionFiles", "WorkersRegistry"]
