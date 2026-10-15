"""state — persistencia atómica del bot (fuente única).

AtomicStore (tmp + fsync + rename + flock) y los registros file-backed
que comparten poller, hooks y workers-PROCESO: WorkersRegistry y
QuestionFiles. Todo el estado multi-proceso del bot vive acá — ver
ADR-003 (dos escritores app↔bot) y ADR-008 (patrones de optimización).

Lección del refactor del 14/10 (commits b7a3d75..57dc336): dos copias
del mismo contrato = una editada y otra ejecutada. UN solo cuerpo vivo
por concepto; los importadores viejos (concurrency.py) son shims.
"""
from .store import AtomicStore
from .registry import QuestionFiles, WorkersRegistry

__all__ = ["AtomicStore", "QuestionFiles", "WorkersRegistry"]
