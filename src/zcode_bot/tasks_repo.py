"""tasks_repo — Repository de solo lectura sobre las bases del CLI/app.

Patrón: Repository. Encapsula el acceso a sqlite (db.sqlite del CLI y
tasks-index.sqlite de la app): conexiones cortas, mode=ro, cero lógica de
dominio. La SQL es EXACTAMENTE la que el monolito usaba (el orden del
sidebar de la app depende de ella — congelado por golden tests).
"""
import contextlib
import json
import os
import sqlite3


@contextlib.contextmanager
def _db(path, ro=True):
    if not os.path.exists(path):
        yield None
        return
    uri = "file:%s?mode=ro" % path if ro else path
    con = sqlite3.connect(uri, uri=ro, timeout=2)
    try:
        yield con
    finally:
        con.close()


def ctx_tasks(base, limit=10):
    """Tasks con consumo, orden sidebar (pinned → updated_at desc),
    deduplicadas de filas legacy. Mismo resultado que el monolito."""
    dbp = os.path.join(base, "cli", "db", "db.sqlite")
    idxp = os.path.join(base, "v2", "tasks-index.sqlite")
    usage = {}
    with _db(dbp) as db:
        if db is None:
            return []
        for sid, inp, out, model, ms in db.execute(
            "SELECT m.session_id, m.input_tokens, m.output_tokens, m.model_id, m.started_at "
            "FROM model_usage m JOIN (SELECT session_id, MAX(started_at) ms FROM model_usage "
            "GROUP BY session_id) l ON l.session_id=m.session_id AND l.ms=m.started_at "
            "WHERE m.session_id NOT LIKE 'sess_subagent%'"
        ):
            usage[sid] = {"ctx": (inp or 0) + (out or 0), "model": model, "when": ms}
    tasks = []
    with _db(idxp) as idx:
        if idx is None:
            return []
        rows = idx.execute(
            "SELECT task_id, title, workspace_path, updated_at, "
            "(workspace_key = workspace_path) AS es_nueva "
            "FROM tasks WHERE deleted=0 AND archived=0 "
            "ORDER BY es_nueva DESC, pinned DESC, updated_at DESC"
        ).fetchall()
    vistos = set()
    for tid, title, ws, _ua, _nueva in rows:
        if tid in vistos:
            continue  # fila legacy del mismo task
        vistos.add(tid)
        if tid not in usage:
            continue  # sin consumo → sin contexto
        u = usage[tid]
        tasks.append({"n": len(tasks) + 1, "sid": tid, "title": title or "",
                      "ws": ws, "ctx": u["ctx"], "model": u["model"],
                      "when": u["when"]})
        if len(tasks) >= limit:
            break
    return tasks


def session_title(base, sid):
    with _db(os.path.join(base, "cli", "db", "db.sqlite")) as db:
        if db is None:
            return None
        try:
            row = db.execute("SELECT title FROM session WHERE id=?",
                             (sid,)).fetchone()
        except sqlite3.Error:
            return None
    return row[0] if row else None


def model_selection(base, sid):
    """Última runtime/model_selection persistida (data JSON) o None."""
    with _db(os.path.join(base, "cli", "db", "db.sqlite")) as db:
        if db is None:
            return None
        try:
            row = db.execute(
                "SELECT data FROM session_entry WHERE session_id=? "
                "AND type='runtime/model_selection' "
                "ORDER BY time_created DESC LIMIT 1", (sid,)).fetchone()
        except sqlite3.Error:
            return None
    return row[0] if row else None


def model_selection_payload(base, sid):
    raw = model_selection(base, sid)
    if not raw:
        return None
    try:
        return json.loads(raw).get("modelSelection")
    except (ValueError, AttributeError):
        return None
