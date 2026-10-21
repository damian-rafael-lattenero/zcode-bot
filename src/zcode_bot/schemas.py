"""schemas — executable documentation of every on-disk format (F54b).

One TypedDict per persisted file. These are the CONTRACTS the P6
migration made bilingual: every writer emits the English keys below and
every reader ALSO accepts the legacy Spanish fallback noted in each
docstring — a shape change here is a wire-format change: update the
writer, keep the reader's fallback, and note it in docs/TECH_DEBT.md.

tests/test_schemas.py pins the required-key set of each TypedDict and
checks the entity to_dict() outputs against them, so key drift fails
the suite instead of aging silently on disk.

stdlib typing only; no I/O at import — safe to import from anywhere.
"""
from typing import Any, Dict, List, Optional, TypedDict


class CotCache(TypedDict):
    """cot-<sid>-<t0>.json — the turn's CoT boxes (reasoning.save_cache).
    Legacy fallbacks readers must keep accepting forever: titulo/cajas/
    rotulos (P6 rename), vivo/finalizado (P6 rename) and bloques
    (pre-F46 boxes). phs arrived in F46; ph is the pre-F46 single box."""
    sid: str
    t0: int
    title: str
    boxes: List[str]
    labels: List[str]


class CotCacheExtra(TypedDict, total=False):
    live: bool
    done: bool
    ph: Optional[int]
    phs: Dict[str, int]
    error: str


class QueueItemShape(TypedDict):
    """bot-queue.json — one queued prompt (workers._queue_put).
    Legacy: pre-F30 items carry no ts."""
    act: Dict[str, Any]      # a BotSession dict
    text: str
    ts: float


class WorkersEntry(TypedDict, total=False):
    """bot-workers.json — live worker registry (state/registry.py).
    Core keys written by WorkerEntry.to_dict; stopped/waiting are added
    by flag_stop/heartbeat. Legacy: titulo (P6 rename, entry_title
    reads both)."""
    t0: float
    prompt: str
    ws: str
    title: str
    pid: int
    stopped: bool
    waiting: bool


class ActiveTurn(TypedDict, total=False):
    """bot-active.json — the in-flight turn (workers._active_turn_register);
    the poller's supervisor delivers the ending if the worker dies
    without its finally. stop is set by bot_stop's flag. Legacy: titulo."""
    title: str
    t0: float
    pid: int
    ws: Optional[str]
    last_id: Optional[int]
    stop: bool


class QuestionFile(TypedDict):
    """bot-q/q-<rid>.json — a pending question (QuestionFiles.ask)."""
    request_id: str
    questions: List[Any]
    title: str
    sid: Optional[str]
    t0: float


class AnswerFile(TypedDict):
    """bot-q/a-<rid>.json — its answer (QuestionFiles.answer)."""
    request_id: str
    answer: str


class AuditEntry(TypedDict, total=False):
    """bot-audit.json — failure log (audit.record; legacy file name
    bot-auditoria.json is read until the new one exists). Legacy keys
    tipo/detalle/clave normalize to type/detail/key in audit.read().
    Legacy kind VALUES (normalized by audit.LEGACY_KINDS): turno-muerto,
    marker-huerfano, cola-dormida, cot-huerfano, fantasma|, alerta,
    envio, espectador."""
    ts: float
    sid: Optional[str]
    type: str
    detail: str
    key: str


class NuclearReportShape(TypedDict):
    """bot-nuclear-report.json — the only contract between the nuclear
    body and the detached watchdog (nuclear.NuclearReport.to_dict).
    Legacy: frenadas/totales/cola/vivos (P6 rename, from_dict reads
    both)."""
    old_pid: int
    t0: float
    dur: int
    stopped: int
    total: int
    mac: int
    queued: int
    alive: int
    app_ok: bool
    notes: List[str]


class SessionShape(TypedDict, total=False):
    """bot-session.json — the active conversation (core.set_active_bot_
    session). model/mode/mode_set only go into the JSON when set (F22:
    they survive the /task switch)."""
    sid: str
    title: str
    ws: str
    model: Dict[str, Any]
    mode: str
    mode_set: str
