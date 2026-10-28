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
