"""cli_logs — scanner parametrizable de los logs JSONL del CLI.

Patrón: Strategy. Unifica los 4 loops de escaneo duplicados del monolito
(_turn_ends_since, _compactiones, _wait_cli_event, _turn_end_poll) en una
sola iteración configurable por filtros.

Los logs viven en <base>/cli/log/zcode-YYYY-MM-DD.jsonl con eventos JSON
por línea: {"timestamp": ISO-8601-UTC, "event": "...", "sessionId": "...",
"context": {...}}.
"""
import datetime
import glob
import json
import os


def _parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iter_events(base, needles=(), sids=None, since_epoch=None,
                last_files=2, tail_bytes=512 * 1024):
    """Itera eventos de los últimos N logs del CLI, ya parseados y
    filtrados, en orden cronológico de archivo (viejo→nuevo).

    needles: tupla de prefijos de evento ("turn.", "compact.completed").
    sids: set de sessionIds que interesan (None = todos, sin subagentes).
    since_epoch: ts mínima (epoch segs, con 1 s de tolerancia).
    tail_bytes: del último archivo se lee solo la cola (los turnos vivos
    están al final; leer entero es O(historia)).
    """
    files = sorted(glob.glob(os.path.join(base, "cli", "log", "zcode-*.jsonl")))
    for path in files[-last_files:] if last_files else files:
        es_ultimo = (path == files[-1]) if files else False
        try:
            if es_ultimo and tail_bytes:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - tail_bytes))
                    f.readline()  # descarta línea parcial de la cola
                    data = f.read().decode("utf-8", errors="replace")
            else:
                with open(path, errors="replace") as f:
                    data = f.read()
        except OSError:
            continue
        for line in data.splitlines():
            if not any(n in line for n in needles):
                continue  # filtro barato ANTES del json.loads
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ev = str(d.get("event") or "")
            if needles and not any(ev.startswith(n) for n in needles):
                continue
            sid = str(d.get("sessionId") or "")
            if sid.startswith("sess_subagent"):
                continue
            if sids is not None and sid not in sids:
                continue
            ts = _parse_ts(d.get("timestamp"))
            if ts is None:
                continue
            if since_epoch is not None and ts < since_epoch - 1:
                continue
            yield ts, sid, ev, d
