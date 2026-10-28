"""brain_client — the bot's door to agent-brain over HTTP (ADR-019, P2).

Standard library only (urllib): the bot does not grow a dependency to talk
to its own brain. Retrieval only, never generation: the brain answers with
ranked chunks and citations; what the chat shows is a reading list, what a
headless turn gets is context. Knobs: `brain_url`, `brain_timeout_s`.
"""
import json
import urllib.error
import urllib.request

PATH_QUERY = "/rag/query"
PATH_STATS = "/rag/stats"
PATH_BUS = "/bus/status"
PATH_READY = "/ready"
PATH_MEMORY_SEARCH = "/memory/search"
PATH_MEMORY_RECENT = "/memory/recent"
PATH_MEMORY_STATUS = "/memory/status"
MEMORY_BLOCK_HEADER = "[memory · agent-brain · %d item(s) · ids %s]"
MEMORY_BLOCK_FOOTER = "—"


class BrainUnavailable(RuntimeError):
    """The brain did not answer (not running, wrong URL, slow)."""


def _url(cfg, path):
    return str(cfg.get("brain_url") or "").rstrip("/") + path


def _call(cfg, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_url(cfg, path), data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=float(cfg.get("brain_timeout_s"))) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {"error": str(e)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BrainUnavailable("%s (%s)" % (_url(cfg, path), e))


def query(cfg, question, k):
    """→ the brain's QueryResponse dict. Raises BrainUnavailable; a 4xx
    answer comes back as {"error": ...} with its status."""
    status, body = _call(cfg, PATH_QUERY, {"query": question, "k": int(k)})
    if status != 200:
        raise BrainUnavailable("HTTP %d: %s" % (status, (body or {}).get("error", body)))
    return body


def stats(cfg):
    """→ (ready, rag_stats, bus_status): a /brain status snapshot."""
    _, ready = _call(cfg, PATH_READY)
    _, rag = _call(cfg, PATH_STATS)
    _, bus = _call(cfg, PATH_BUS)
    return ready, rag, bus


def memory_search(cfg, query, k):
    """→ the brain's MemorySearchResponse dict (memories: [{id, kind, content, ...}])."""
    status, body = _call(cfg, PATH_MEMORY_SEARCH, {"query": query, "k": int(k)})
    if status != 200:
        raise BrainUnavailable("HTTP %d: %s" % (status, (body or {}).get("error", body)))
    return body


def memory_recent(cfg, k):
    status, body = _call(cfg, PATH_MEMORY_RECENT + "?limit=%d" % int(k))
    if status != 200:
        raise BrainUnavailable("HTTP %d: %s" % (status, body))
    return body


def memory_status(cfg):
    status, body = _call(cfg, PATH_MEMORY_STATUS)
    if status != 200:
        raise BrainUnavailable("HTTP %d: %s" % (status, body))
    return body


def memory_context(cfg, text):
    """ADR-020: the memory block prepended to a headless prompt → (block, ids).
    Bounded (memory_inject_k items, memory_inject_max_chars), cited (one id
    per line), best effort: knob off, brain down or empty → ("", [])."""
    if not cfg.get("memory_inject") or not (text or "").strip():
        return "", []
    try:
        memories = (memory_search(cfg, text, cfg.get("memory_inject_k")) or {}).get("memories") or []
    except BrainUnavailable:
        return "", []
    lines, ids, used = [], [], 0
    cap = int(cfg.get("memory_inject_max_chars"))
    for m in memories:
        line = "- %s #%s: %s" % (m.get("kind"), m.get("id"), str(m.get("content") or "").strip())
        if used + len(line) + 1 > cap:
            break
        lines.append(line)
        ids.append(m.get("id"))
        used += len(line) + 1
    if not lines:
        return "", []
    header = MEMORY_BLOCK_HEADER % (len(lines), ",".join(str(i) for i in ids))
    return header + "\n" + "\n".join(lines) + "\n" + MEMORY_BLOCK_FOOTER + "\n", ids
