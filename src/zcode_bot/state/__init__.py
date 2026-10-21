"""state — the bot's atomic persistence (single source).

AtomicStore (tmp + fsync + rename + flock) and the file-backed
registries shared by poller, hooks and PROCESS-workers: WorkersRegistry
and QuestionFiles. All of the bot's multi-process state lives here —
see ADR-003 (two writers app↔bot) and ADR-008 (optimization patterns).

Lesson from the 14/10 refactor (commits b7a3d75..57dc336): two copies
of the same contract = one edited, another executed. ONE living body
per concept; the old importers (concurrency.py) are shims.
"""
from .store import AtomicStore
from .registry import QuestionFiles, WorkersRegistry

__all__ = ["AtomicStore", "QuestionFiles", "WorkersRegistry"]
