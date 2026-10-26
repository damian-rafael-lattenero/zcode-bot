"""reasoning — CoT in SIZE-based BOXES, with buttons (F40-F44).

Damian's model (F43, verbatim): "a box has a size; when the CoT reaches
the cap the box is full and is sent with its number; the CoT continues
and brakes at the cap again, and so buttons arrive incrementally until
the CoT ends; then the chat writes or sends a form; and when it thinks
again, it sends a numbered CoT again starting from 1". That is: BOXES
SEALED BY SIZE during the reasoning pass (not by phase), a button per
box as soon as it fills, and numbering that RESTARTS on every reasoning
pass.

Sources (all three write the same cache and use the same buttons):
- F42 phone: LiveTurnView feeds a CotSealer with the reasoning_delta.
- F41 Mac: `--cot-live` polls the history (the reasoning part of the step
  in progress is already readable mid-step — verified live 20/10).
  F44: the watcher stays WARM between turns of the same session — ONE
  resume per session, not per turn (bot_development's resume took ~45s
  and the first little box arrived at ~48s into EVERY prompt). The
  pid-file cot-live-<sid>.pid (pid + heartbeat in the mtime) is the
  hook's single-flight: with a live, fresh watcher no other is spawned.
- F40 Mac (close-time fallback): `--cot-buttons` captures post-turn.

The tap (ct:sid:t0:n[:p] → /cotb) reads the cache: no runtime, instant.
COT_BOX_CAP = COT_PART_CAP → each box opens WHOLE in one message, no
part sub-buttons. The cache stores `ph` (the message_id of the button
box) so the tap edits IN the message (F43c): send returns that id and
every cache rewrite preserves it.

F45: a turn that closes WITHOUT reasoning says so in one line
(empty_text) instead of staying silent — all three sources. Diagnosis
from 20/10 over the runtime's raw rollout: the bot requests IDENTICAL
thinking for GLM-5.3 and Flash (thinking enabled + effort max); GLM-5.3
reasons ADAPTIVELY — it decides per call — and in the bot's session it
went 12 calls in a row without reasoning at ~130k tokens of context (it
came back after compacting); Flash reasons almost always. Without the
notice, zero little boxes was indistinguishable from a bug.
"""
import datetime
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from zcode_bot import config
from zcode_bot.config import marker_path
from zcode_bot.core import _stop_requested, _task_current_ok, _worker_owned
from zcode_bot import proto as zc_proto
from zcode_bot import verbosity
from zcode_bot.messages import _fetch_messages, _last_id_of
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, equal_parts, fmt_tok, task_label
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import pwlog

COT_PART_CAP = 3000      # chars per part when OPENING an old/large box
COT_BOX_CAP = COT_PART_CAP  # chars per BOX: at the cap it is sealed and its button travels
COT_BUTTONS_MAX = 20     # keyboard: more boxes → /cot note (document)
COT_BUTTONS_ROW = 5
COT_MSGS = 60            # messages to fetch: the longest Mac turn fits
MAC_BEAT_S = 45         # F70: the pipe beat (was 180 — Damian's base
#                        requirement: visible circulation at every moment)
COT_RESUME_TIMEOUT_S = 120
COT_CACHE_FMT = "cot-%s-%d.json"
COT_PID_FMT = "cot-live-%s.pid"  # F44: pid of the session's warm watcher
# P6: the pid-file name and the cache keys below were Spanish until P6
# (cot-vivo-*.pid; titulo/cajas/rotulos/vivo/finalizado/bloques). Writes  # noqa: spanish — old cache shape, readers stay bilingual
# are English-only since then; the READERS stay bilingual forever — a
# 24h-old Telegram button must still open its box (the on-disk caches it
# points at were written by the pre-deploy code).
_PID_PREFIXES = ("cot-live-", "cot-vivo-")  # dual glob: old files age out


def cache_path(sid: str, t0: float) -> str:
    return os.path.join(config.STATE_DIR, COT_CACHE_FMT % (sid, int(t0)))


def ph_of(res: Any) -> Optional[int]:
    """Usable message_id from what uhttp.send returned: True (sent but
    without an id) is no good for editing — and bool IS a subclass of int."""
    return res if isinstance(res, int) and not isinstance(res, bool) else None


def _with_ph(ph):
    return {"ph": ph} if ph else {}


# ------------------------------------------------------------- the sealer

class CotSealer:
    """Seals boxes by SIZE within ONE reasoning pass: feed() returns
    the boxes that filled (with their local number, from 1); close()
    seals the tail as the pass's last box and RESETS the state for the
    next one. The caller builds the VISIBLE label: «CoT R.C» (F46) —
    reasoning pass R, box C within it — so there are never two «CoT 1».
    No odd states: the accumulated text is kept whole until the pass
    closes."""

    def __init__(self, cap: int = COT_BOX_CAP) -> None:
        self.cap = cap
        self.text = ""
        self.sealed = 0

    def feed(self, delta: str) -> List[Tuple[str, int]]:
        """[(box, n_local)] of the boxes that reached the cap."""
        self.text += delta
        filled = []
        while len(self.text) >= self.cap * (self.sealed + 1):
            self.sealed += 1
            cut = self.cap * self.sealed
            filled.append((self.text[cut - self.cap:cut],
                           self.sealed))
        return filled

    def close(self) -> List[Tuple[str, int]]:
        """End of the reasoning pass: the tail (if it has anything) is
        the last box. State resets for the next pass."""
        tail = self.text[self.cap * self.sealed:]
        n = self.sealed + 1
        self.text, self.sealed = "", 0
        return [(tail, n)] if tail.strip() else []

    @property
    def in_progress(self) -> Tuple[int, int]:
        """(chars of the box currently filling, that box's n_local)."""
        rest = len(self.text) - self.cap * self.sealed
        return max(0, rest), self.sealed + 1


def text_boxes(text: str, cap: int = COT_BOX_CAP) -> List[Tuple[str, int]]:
    """Boxes of an ALREADY finished reasoning pass: filled to the cap +
    final tail. [(box, n_local)] with numbering from 1."""
    return [(text[i:i + cap], i // cap + 1)
            for i in range(0, len(text), cap)] if text.strip() else []


# ------------------------------------------- from the history (Mac: F40/F41)

def _turn_parts(msgs):
    """The turn's reasoning parts (after the last user) GROUPED into
    reasoning passes: a pass ends when the model DOES something (a
    tool/text part between reasonings); contiguous reasonings are the
    same pass. [(reasoned_text, in_progress_bool)] — the last one may keep
    growing (it is the last message's last part)."""
    start = 0
    for i, m in enumerate(msgs):
        role = (m.get("info") or {}).get("role") or m.get("role")
        if role == "user":
            start = i + 1
    passes, current, in_progress = [], [], False
    for mi, m in enumerate(msgs[start:]):
        parts = m.get("parts") or []
        for pi, p in enumerate(parts):
            if not isinstance(p, dict):
                continue
            tp = p.get("type")
            is_last_part = (mi == len(msgs[start:]) - 1
                            and pi == len(parts) - 1)
            if tp == "reasoning":
                t = str(p.get("text") or p.get("reasoning") or "")
                if t.strip():
                    current.append(t)
                    in_progress = is_last_part
            elif tp in ("tool", "text") and current:
                # the model did something between reasonings: the pass closed
                passes.append("".join(current))
                current, in_progress = [], False
    if current:
        passes.append("".join(current))
    return passes, in_progress


def _boxes_from_reasoned(reasoned, until=None, base_label=0):
    """(boxes, labels) of the given reasoning passes — the loop that
    turn_boxes and live_boxes were each duplicating. until = only
    the first N (the in-progress one stays out); base_label = initial
    numbering (the live_boxes tail keeps the LAST pass's numbering)."""
    boxes, labels = [], []
    sliced = reasoned[:until] if until is not None else reasoned
    for r, text in enumerate(sliced, base_label + 1):
        for box, n in text_boxes(text):
            boxes.append(box)
            labels.append("CoT %d.%d" % (r, n))
    return boxes, labels


def turn_boxes(msgs: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """COMPLETE boxes of the turn (turn already closed): each reasoning
    pass fills its boxes to the cap + a tail. F46: label «CoT R.C»
    (pass R, box C) — unique, never two «CoT 1». → parallel
    (boxes, labels)."""
    reasoned, _in_progress = _turn_parts(msgs)
    return _boxes_from_reasoned(reasoned)


def live_boxes(msgs: List[Dict[str, Any]]) -> Tuple[List[str], List[str], int, str]:
    """Same as turn_boxes but for the turn IN PROGRESS: the pass
    under way only contributes the boxes ALREADY FILLED (not the one
    filling now). → (boxes, labels, chars_in_progress, label_in_progress
    «R.C»)."""
    reasoned, in_progress = _turn_parts(msgs)
    boxes, labels = _boxes_from_reasoned(
        reasoned, until=len(reasoned) - 1 if in_progress else None)
    if in_progress and reasoned:
        r = len(reasoned)
        text = reasoned[-1]
        filled = len(text) // COT_BOX_CAP
        more_boxes, more_labels = _boxes_from_reasoned(
            [text[:filled * COT_BOX_CAP]], base_label=r - 1)
        boxes += more_boxes
        labels += more_labels
        pending = len(text) % COT_BOX_CAP
        return boxes, labels, pending, "%d.%d" % (r, filled + 1)
    return boxes, labels, 0, ""


# ------------------------------------------------------------- buttons

def _rows(buttons):
    rows, row = [], []
    for b in buttons:
        row.append(b)
        if len(row) == COT_BUTTONS_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def box_buttons(sid: str, t0: float, labels: List[str],
                start: int = 1) -> List[List[Dict[str, str]]]:
    """One button per box; labels «CoT R.C» (F46: never two «CoT 1») and
    the callback_data carries the GLOBAL index: ct:sid:t0:n. start = the
    first label's global index (F46: ONE pass's keyboard is a slice of
    the total)."""
    return _rows([{"text": labels[i - start][:36],
                   "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                  for i in range(start,
                                 start + min(len(labels), COT_BUTTONS_MAX))])


def part_buttons(sid: str, t0: float, n: int, total: int,
                 current: int, prefix: str = "ct:") -> List[List[Dict[str, str]]]:
    return _rows([{"text": ("[%d/%d]" if p == current else "%d/%d")
                   % (p, total),
                   "callback_data": "%s%s:%d:%d:%d" % (prefix, sid, int(t0),
                                                       n, p)}
                  for p in range(1, total + 1)])


def collapse_buttons(sid: str, t0: float, n: int,
                     prefix: str = "cr:") -> List[List[Dict[str, str]]]:
    """F59 — the ⤴️ row under an EXPANDED box: folds the message back to
    its frozen line + ITS single button."""
    return [[{"text": "⤴️ Collapse",
              "callback_data": "%s%s:%d:%d" % (prefix, sid, int(t0), n)}]]


def agent_buttons(sid: str, t0: float, n: int, labels: List[str],
                  start: int = 1) -> List[List[Dict[str, str]]]:
    """F63 — the «Agent N.C» buttons: same shape as box_buttons with the
    ag: vocabulary (the callback carries agent n AND the box index)."""
    return _rows([{"text": labels[i - start][:36],
                   "callback_data": "ag:%s:%d:%d:%d" % (sid, int(t0), n, i)}
                  for i in range(start,
                                 start + min(len(labels), COT_BUTTONS_MAX))])


def box_open_view(sid: str, t0: float, n: int, label: str, title: str,
                  content: str) -> Tuple[str, Dict[str, Any]]:
    """F61 verbose — the box frozen ALREADY OPEN (born open): show()'s
    layout (same header, same part split of the ESCAPED text — esc can
    inflate a raw-capped box past the part cap) with the ⤴️ row under the
    parts row, so /cotr folds it back exactly like a tapped-open box and
    a part tap re-expands in place (F59)."""
    parts = equal_parts(esc(content), COT_PART_CAP)
    header = "🧠 <b>%s</b> · «%s»" % (esc(label), esc(title or "?"))
    rows: List[List[Dict[str, str]]] = []
    if len(parts) > 1:
        header += " · part 1/%d" % len(parts)
        rows = part_buttons(sid, t0, n, len(parts), 1)
    return (header + "\n<pre>%s</pre>" % parts[0],
            {"inline_keyboard": rows + collapse_buttons(sid, t0, n)})


def index_buttons(sid: str, t0: float, labels: List[str],
                  idxs: List[int]) -> List[List[Dict[str, str]]]:
    """F49 — buttons for arbitrary 1-based GLOBAL indexes (the boxes that
    live in ONE message: the pass's own, or the late ones from the
    close); labels is the turn's complete list."""
    return _rows([{"text": labels[i - 1][:36],
                   "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                  for i in idxs[:COT_BUTTONS_MAX]])


def kb_log(source: str, ph: Optional[int],
           kb: Optional[Dict[str, Any]]) -> None:
    """F49 — the feedback that was missing (Damian: «you have no way to
    get feedback»): every send/edit of a button box records in the pwlog
    WHICH buttons that message ended up showing. So the chat can be
    rebuilt from the log without looking at it."""
    tags = [b.get("text", "?") for row in (kb or {}).get(
        "inline_keyboard", []) for b in row]
    pwlog("cot kb %s ph=%s [%d]: %s" % (source, ph, len(tags),
                                        " ".join(tags)))


# ------------------------------------------------------------- cache

def save_cache(sid: str, t0: float, title: str, boxes: List[str],
               labels: List[str], **extra: Any) -> None:
    """Writes/updates a turn's cache (all three sources). extra:
    live/done/error. NEW (P6) English keys {sid, t0, title, boxes,
    labels}; readers accept the OLD Spanish shape (titulo/cajas/rotulos/  # noqa: spanish — legacy cache keys (pre-P6), readers stay bilingual
    vivo/finalizado and the legacy 'bloques') so pre-deploy caches keep
    feeding their buttons. tmp+replace (audit 20/10): the "w" truncated
    the file in front of ANOTHER process (the Mac's watcher writes
    every ~3s while the poller reads on every tap → FALSE «no longer
    saved») and a kill -9 mid-dump left the cache truncated FOREVER —
    the turn lost its buttons for life."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = "%s.tmp.%d" % (cache_path(sid, t0), os.getpid())
    with open(tmp, "w") as f:
        json.dump({"sid": sid, "title": title, "t0": int(t0),
                   "boxes": boxes, "labels": labels, **extra}, f)
    os.replace(tmp, cache_path(sid, t0))


def cache_exists(sid: str, t0: float) -> bool:
    return os.path.exists(cache_path(sid, t0))


def cache_closed_or_live(sid: str, t0: float) -> bool:
    """F51a — did someone publish, or is someone publishing, this turn's
    close? Yes if the cache ended up done (the live one closed; legacy
    finalizado accepted) or there is a watcher PROCESS (pid alive;
    freshness infinite: while resuming it can carry an expired heartbeat
    for up to 120s and still be the owner — possession is marked by the
    live=True cache it wrote, F41). A live=True cache with a DEAD pid is
    a live one that died midway: before, that still suppressed the
    fallback and the turn closed WITHOUT boxes — with the cache lying
    alive forever."""
    try:
        with open(cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict):
        return False
    return bool(d.get("done") or d.get("finalizado")) \
        or watcher_alive(sid, fresh_s=float("inf"))


def close_cache(sid: str, t0: float, **extra: Any) -> bool:
    """F47d — closes the cache of a turn that died WITHOUT its close
    (worker killed off by /stop, kill -9, OOM): the LiveTurnView's last
    save_cache was live=True and that is how it stayed forever. Keeps
    boxes/labels/ph/phs — only removes the live flag (new 'live' and
    legacy 'vivo'), marks done and adds extra (e.g. error=). False if
    there is no cache (turn without boxes) or it could not be
    read/written."""
    try:
        with open(cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict):
        return False
    d.pop("live", None)
    d.pop("vivo", None)  # legacy caches: the old live flag, same meaning
    d["done"] = True
    d.update(extra)
    try:
        tmp = "%s.tmp.%d" % (cache_path(sid, t0), os.getpid())
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, cache_path(sid, t0))
    except OSError:
        return False
    return True


def live_text(title: str, n: int, in_progress: str = "",
              pending: int = 0) -> str:
    """Status of the button box: sealed boxes + the one filling
    (in_progress = «R.C» label of the box being filled)."""
    line = "🧠 <b>«%s»</b> thinking… · %d box%s" % (
        esc(title or "?"), n, "es" if n != 1 else "")
    if pending:
        line += " · filling CoT %s (%d chars)" % (in_progress, pending)
    if n:
        line += "\nTap one to read it:"
    return line


def box_live_text(title: str, r_c: str, pend: int) -> str:
    """F57 — the FILLING box's own live line: its message was born for
    THIS box and edits text-only while it fills (no keyboard: nothing
    ever re-flows for anyone)."""
    return "🧠 <b>«%s»</b> thinking… · filling CoT %s (%d chars)" % (
        esc(title or "?"), r_c, pend)


def box_done_text(label: str, size: int) -> str:
    """F57 — the seal: the box's message solidifies with this line and
    ITS single button, and is never touched again."""
    return "🧠 <b>%s</b> · %s" % (esc(label), fmt_tok(size))


def box_cut_text(title: str, r_c: str) -> str:
    """F57 — the turn ended while this box was filling: its live message
    closes as a plain cut line (no sealed box, nothing to tap)."""
    return "🧠 <b>«%s»</b> · CoT %s cut (the turn ended while filling)" % (
        esc(title or "?"), r_c)


def reasoned_text(title: str, r: int, n: int, total: int) -> str:
    """F46 — close of ONE reasoning pass: its button box keeps its own
    final status (the turn goes on: the next pass is born in a new
    message, in its chronological position)."""
    return ("🧠 <b>«%s»</b> · pass %d — %d box%s, %d chars. "
            "Tap one to read it:"
            % (esc(title or "?"), r, n, "es" if n != 1 else "", total))


def final_text(title: str, t0: float, n: int, total: int) -> str:
    hhmm = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    text = ("🧠 <b>Reasoning</b> of «%s» (turn at %s) — %d box%s, "
            "%d chars. Tap one to read it:"
            % (esc(title or "?"), hhmm, n, "es" if n != 1 else "", total))
    if n > COT_BUTTONS_MAX:
        text += ("\n(buttons for the first %d; the full document "
                 "with /cot)" % COT_BUTTONS_MAX)
    return text


def late_text(title: str, n: int, t0: float,
              when: str = "as the turn closed") -> str:
    """F49 — boxes whose reasoning pass never got its own message (send
    failed on the phone; passes born in the Mac's close-time fetch): ONE
    message at the end with THOSE — no button is lost and nothing piles
    up (the old global index: «they all piled up in the last box»).
    F51a: 📥 + «turn of HH:MM» — it lands at the close and without an
    anchor it read as another turn's activity (incident 21/10 08:18: the
    07:43 turn's boxes falling onto a just-started turn).
    F72: when — the moment the bulk is landing (the close, or the
    re-attach of a stream that was muted mid-turn)."""
    hhmm = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    return ("📥 🧠 <b>«%s»</b> · %d box%s %s — turn at "
            "%s. Tap one to read it:"
            % (esc(title or "?"), n, "es" if n != 1 else "", when, hhmm))


def empty_text(title: str, t0: float) -> str:
    """F45 — the turn closed and there was NOT ONE reasoning part/delta:
    one line to say so. Without this the chat stayed mute and zero little
    boxes was indistinguishable from a bug (Damian, 20/10: 'fix it so
    there is cot on both models' — the bot already requested thinking
    the same for both)."""
    hhmm = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    return ("🧠 <b>«%s»</b> (turn at %s) — <b>no CoT</b>: the model did "
            "not reason this turn (adaptive thinking: it decides per call; "
            "GLM-5.3 often skips it with a very long context — compacting "
            "brings it back; Flash reasons almost always)."
            % (esc(title or "?"), hhmm))


# ------------------------------------------------------------- sources

def capture(cfg: Dict[str, Any], sid: str, title: str, t0: float,
            ws: str) -> Optional[int]:
    """F40 — fallback when a Mac turn closes (if the live one could not):
    fetches the history, caches the boxes and sends the message with
    buttons. Returns the box count: 0 if the turn closed without
    reasoning (F45: announced in one line), None if the resume failed or
    there was no history."""
    c = zc_proto.make(cwd=ws)
    try:
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
        if e := zc_proto.proto_err(r, "resume"):
            pwlog("cot-buttons %s: %s" % (sid[-8:], e[:120]))
            return None
        msgs = _fetch_messages(c, sid, limit=COT_MSGS) or []
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    boxes, labels = turn_boxes(msgs)
    if not boxes:
        if not msgs:
            return None
        if cfg.get("cot_empty_notice"):
            # F51a: 📥 — this message arrives DELAYED (the fallback paid
            # for the history resume); the prefix tells it apart from the
            # watcher's live close.
            uhttp.send(cfg, "📥 " + empty_text(title or sid[-8:], t0),
                       loud=False)
        return 0
    save_cache(sid, t0, title, boxes, labels)
    total = sum(len(b) for b in boxes)
    ph = ph_of(uhttp.send(cfg, "📥 " + final_text(title or sid[-8:], t0,
                                                  len(boxes), total),
                          loud=False,
                          markup={"inline_keyboard": box_buttons(sid, t0,
                                                                  labels)}))
    if ph:
        save_cache(sid, t0, title, boxes, labels, ph=ph)  # F43c
    return len(boxes)


def show(sid: str, t0: float, n: int,
         part: int = 1,
         cache_p: Optional[str] = None) -> Union[str, Tuple[str, Dict[str, Any]]]:
    """The tap: box n from the cache (local label; global index on the
    button). cache_p: a DIFFERENT cache file (F63 agent boxes) — the
    shape is the same, the path is the only difference."""
    try:
        with open(cache_p or cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ("🤷 That reasoning is no longer saved (purged after "
                "2 days) — /cot brings the task's full document.")
    boxes = d.get("boxes") or d.get("cajas") or d.get("bloques") or []  # noqa: spanish — legacy cache keys, permanent bilingual read
    if n < 1 or n > len(boxes):
        return "⚠️ Box out of range (1 to %d)." % len(boxes)
    labels = d.get("labels") or d.get("rotulos") or []  # noqa: spanish — legacy cache keys, permanent bilingual read
    label = labels[n - 1] if len(labels) >= n else "CoT %d" % n
    # (audit 20/10) escape BEFORE splitting: parts split by RAW cap
    # inflated up to 5x with esc() and send()'s truncation silently cut
    # the box — the box must open WHOLE (the part keyboard cannot lie
    # about what is on screen)
    parts = equal_parts(esc(boxes[n - 1]), COT_PART_CAP)
    part = min(max(part, 1), len(parts))
    header = "🧠 <b>%s</b> · «%s»" % (esc(label),
                                      esc(d.get("title") or d.get("titulo")  # noqa: spanish — legacy cache key
                                          or sid[-8:]))
    if len(parts) == 1:
        return header + "\n<pre>%s</pre>" % parts[0]
    header += " · part %d/%d" % (part, len(parts))
    return (header + "\n<pre>%s</pre>" % parts[part - 1],
            {"inline_keyboard": part_buttons(sid, t0, n, len(parts),
                                             part)})


# ------------------------------------------------------------- F41: live Mac

COT_LIVE_POLL_S = 3.0     # cadence of the history polling
COT_LIVE_MAX_S = 35 * 60  # watcher ceiling (the same as the spectator's)
COT_LIVE_WAIT_S = 1.0     # F44: cadence of the warm wait (small files, no runtime)
COT_LIVE_HB_FRESH_S = 90  # F44: heartbeat older than this = hung watcher (> the 60s
#                          session/messages timeout: a slow fetch is not a hang)
COT_LIVE_EMPTY_POLLS = 5  # F44: consecutive polls with NOTHING after after_id (~15s) → re-resume
#                          (the new prompt must be there: if it is not seen, the
#                          connection is not seeing the store — a resume refreshes it)
YIELDED = object()        # F44: a successor took the pid-file — this watcher steps down
LIVE_CEILING = object()   # F44: 35 min and the marker is still there — the same turn is not watched again


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", sid or "ses")


def pid_path(sid: str) -> str:
    return os.path.join(config.STATE_DIR, COT_PID_FMT % _safe_sid(sid))


def pid_write(sid: str) -> None:
    """The watcher registers itself: its pid in the file, its heartbeat
    in the mtime. The hook reads it to avoid spawning a second
    watcher."""
    with open(pid_path(sid), "w") as f:
        f.write(str(os.getpid()))


def pid_beat(sid: str) -> None:
    try:
        os.utime(pid_path(sid), None)
    except OSError:
        pass


def pid_read(sid: str) -> int:
    for name in (COT_PID_FMT % _safe_sid(sid),
                 "cot-vivo-%s.pid" % _safe_sid(sid)):  # pre-F53 watcher
        try:
            with open(os.path.join(config.STATE_DIR, name)) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            continue
    return 0


def pid_remove(sid: str) -> None:
    """Only if the file is still MINE: a successor may have taken it over."""
    if pid_read(sid) == os.getpid():
        try:
            os.remove(pid_path(sid))
        except OSError:
            pass


def _yielded(sid):
    """Another pid in the pid-file = a successor took the session (the
    hook saw the stale heartbeat). No file, no successor."""
    return pid_read(sid) not in (0, os.getpid())


def watcher_alive(sid: str, proc_alive: Optional[Callable[[int], bool]] = None,
                  now: Optional[Callable[[], float]] = None,
                  fresh_s: float = COT_LIVE_HB_FRESH_S) -> bool:
    """F44 single-flight for the hook: is there a warm watcher for this
    session? pid-file with a live pid and a fresh heartbeat. Dead or
    hung → False: the hook spawns another one, which takes the pid-file."""
    pid = pid_read(sid)
    if not pid:
        return False
    try:
        age = (now or time.time)() - os.path.getmtime(pid_path(sid))
    except OSError:
        return False
    if age > fresh_s:
        return False
    if proc_alive is None:
        from zcode_bot.workers import _proc_alive  # lazy: workers → live_view → here
        proc_alive = _proc_alive
    return bool(proc_alive(pid))


def watcher_pids() -> List[int]:
    """pids from the cot-live-*.pid pid-files (P6 dual glob: the legacy
    cot-vivo-*.pid too — old files age out in minutes): /nuclear kills
    them off along with the workers (checking the cmdline: an orphaned
    file can point to a recycled pid)."""
    try:
        names = os.listdir(config.STATE_DIR)
    except OSError:
        return []
    pids = []
    for name in names:
        if not (name.startswith(_PID_PREFIXES) and name.endswith(".pid")):
            continue
        try:
            with open(os.path.join(config.STATE_DIR, name)) as f:
                pid = int(f.read().strip() or 0)
        except (OSError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    return pids


def watchers_cleanup() -> None:
    try:
        names = os.listdir(config.STATE_DIR)
    except OSError:
        return
    for name in names:
        if name.startswith(_PID_PREFIXES) and name.endswith(".pid"):
            try:
                os.remove(os.path.join(config.STATE_DIR, name))
            except OSError:
                pass


def _marker_state(sid):
    """(mtime_int, token) of the in-flight turn's marker; None if there
    is no turn. token "" = marker mid-write (the hook truncates then
    writes): it resolves on the next poll, it is not a new turn."""
    p = marker_path(sid)
    try:
        mt = int(os.path.getmtime(p))
    except OSError:
        return None
    try:
        with open(p) as f:
            tok = str((json.load(f) or {}).get("token") or "")
    except (OSError, ValueError):
        tok = ""
    return mt, tok


def _other_turn(start, state):
    """Is the marker I see from a DIFFERENT turn than the one I started
    watching? By token (unique per prompt) when both were read; by mtime
    otherwise."""
    (mt0, tok0), (mt, tok) = start, state
    if tok0 and tok:
        return tok != tok0
    return mt != mt0


def _parse_reasoned_label(label):
    """«CoT R.C» → R (0 if it does not parse: old «CoT N» labels with no dot)."""
    try:
        rc = str(label).split()[1]
        return int(rc.split(".")[0]) if "." in rc else 0
    except (IndexError, ValueError):
        return 0


def _phs_of(labels, phs_map):
    """F46 — list of message_id per box (aligned to labels): the tap
    edits ITS pass's message; None where there was no message."""
    return [phs_map.get(_parse_reasoned_label(l)) for l in labels]


def _rc_of(label: str) -> str:
    """F57 — the «R.C» core of a box label, bare or with the «CoT »
    prefix: the display paths birth the live message with the bare core
    (box_live_text/box_cut_text of both sides) while add/sync compare
    against the full «CoT R.C» — both forms must capture the freeze."""
    s = str(label)
    return s[4:] if s.startswith("CoT ") else s


class BoxLedger:
    """F54d — the pure box/round bookkeeping the Mac watcher
    (_watch_turn) and the phone's live view (LiveTurnView) share:
    parallel boxes/labels lists, round numbering («CoT R.C» labels), the
    round-on-screen state and the per-round message ids the cache
    persists (round_phs is the single source of truth — the per-box phs
    list is DERIVED via _phs_of, since every box of a round points at
    that round's message). No I/O, no Telegram: each side owns its
    display and feeds this from its own source — the phone APPENDS
    sealed boxes as the stream delivers them; the Mac REPLACES the whole
    list each poll (history slice) and asks what changed."""

    def __init__(self) -> None:
        self.boxes: List[str] = []
        self.labels: List[str] = []
        self.round = 0          # on screen (0 = the anti-silence adopt, Mac)
        self.round_start = 1    # 1-based global index of its first box
        self.round_ph: Optional[int] = None
        self.round_phs: Dict[int, int] = {}   # round -> message id (parked)
        # F57 — one message per box: born "thinking… CoT R.C" at the box's
        # START (live), frozen with its button at the seal. box_phs holds
        # each SEALED box's message id; live_ph/live_label track the one
        # being filled right now.
        self.box_phs: List[Optional[int]] = []
        self.live_ph: Optional[int] = None
        self.live_label: str = ""

    # ------------------------------------------------ shared
    def label(self, n: int) -> str:
        """«CoT R.C» of the round-on-screen's n-th box."""
        return "CoT %d.%d" % (self.round, n)

    def round_count(self) -> int:
        """Boxes of the round on screen."""
        return len(self.boxes) - self.round_start + 1

    def round_span(self, r: int) -> Optional[Tuple[int, int]]:
        """1-based [start, end) of a round's boxes, from the labels."""
        idx = [i for i, l in enumerate(self.labels, 1)
               if _parse_reasoned_label(l) == r]
        return (idx[0], idx[-1] + 1) if idx else None

    def set_round_ph(self, ph: Optional[int]) -> None:
        """The round on screen got (or lost) its message id. Round 0
        (the anti-silence adopt) never parks: it is not a real round."""
        self.round_ph = ph
        if ph is not None and self.round:
            self.round_phs[self.round] = ph

    def cache_kwargs(self) -> Dict[str, Any]:
        """The save_cache quartet with phs ALWAYS up to date (R9). F57:
        per-box message ids when the new model populated them — flagged
        «perbox» (F59: only those boxes expand IN their own message on
        tap; the legacy derived-from-rounds phs share a round message
        and keep opening in the reader); the derived shape only for
        legacy callers."""
        if self.box_phs:
            return {"boxes": list(self.boxes), "labels": list(self.labels),
                    "phs": list(self.box_phs), "perbox": True}
        return {"boxes": list(self.boxes), "labels": list(self.labels),
                "phs": _phs_of(self.labels, self.round_phs)}

    def live_birth(self, ph: int, label: str) -> None:
        """The filling box's message is born (F57 anti-silence: at the
        box's START, not at its seal)."""
        self.live_ph = ph
        self.live_label = label

    # ------------------------------------------------ phone path (append)
    def add(self, box: str, label: str) -> Optional[int]:
        """A sealed box (from the stream). The message that was LIVING
        for this box becomes its frozen id; returns it (None = it was
        never born — latecomer candidate)."""
        self.boxes.append(box)
        self.labels.append(label)
        ph = self.live_ph if _rc_of(self.live_label) == _rc_of(label) else None
        self.box_phs.append(ph)
        self.live_ph = None
        self.live_label = ""
        return ph

    def next_round(self) -> Optional[Tuple[int, int, int]]:
        """Close the round on screen: returns (r, start, end) for its
        close display — None when it sealed no boxes (F48c: it consumes
        no round number and no message)."""
        if self.round_count() <= 0:
            return None
        r, start = self.round, self.round_start
        end = len(self.boxes) + 1
        if self.round_ph is not None:
            self.round_phs[r] = self.round_ph
        self.round, self.round_start = r + 1, end
        self.round_ph = None
        return r, start, end

    # ------------------------------------------------ Mac path (replace+diff)
    def sync(self, boxes: List[str],
             labels: List[str]) -> Tuple[List[Tuple[int, int]],
                                         List[Tuple[int, int]]]:
        """Replace the lists with the whole history slice. Returns
        (starts, newly_frozen): the round starts among the NEW boxes
        (numbering only — F57 has no round messages), and the (index0,
        ph) pairs of new boxes that captured their live message (the
        caller freezes them with their button). Bulk arrivals beyond the
        first get None (latecomer fallback at close). Caller keeps the
        monotonic guard (never sync fewer boxes than were shown)."""
        seen = len(self.boxes)
        starts: List[Tuple[int, int]] = []
        frozen: List[Tuple[int, int]] = []
        cur = self.round
        new_phs: List[Optional[int]] = []
        for i in range(seen, len(labels)):
            label = labels[i]
            r = _parse_reasoned_label(label)
            if r and r != cur:
                starts.append((i, r))
                cur = r
            if self.live_ph and _rc_of(label) == _rc_of(self.live_label):
                frozen.append((i, self.live_ph))
                new_phs.append(self.live_ph)
                self.live_ph = None
                self.live_label = ""
            else:
                new_phs.append(None)
        # F65b — MONOTONIC sync: the fork's history REWRITES the step in
        # flight and the reasoning part can SHRINK mid-turn (verified live
        # 25/10: 6 boxes at 16:52 → 2 in the close-time fetch; the store
        # settles only later). A shorter history is a lie: what was
        # already seen stays (the guard the poll loop had, MOVED here so
        # the close-time fetch cannot clobber the cache either — that is
        # how the turn lost its boxes for good).
        if len(labels) < len(self.boxes):
            boxes = list(self.boxes)
            labels = list(self.labels)
        self.boxes, self.labels = list(boxes), list(labels)
        self.box_phs = (self.box_phs[:seen] + new_phs)[:len(labels)] \
            if len(labels) >= seen else list(self.box_phs[:len(labels)])
        return starts, frozen

    def advance_to(self, r: int, index0: int) -> Tuple[Optional[int], Optional[int]]:
        """A new round takes the screen. Returns (closed_round,
        closed_ph) for ITS close edit — (None, None) when the on-screen
        message was the anti-silence adopt (round 0): F49 says it is
        ADOPTED as the new round's message, so its ph carries over and
        nothing closes."""
        if self.round:
            closed = (self.round, self.round_ph)
            if self.round_ph is not None:
                self.round_phs[self.round] = self.round_ph
            self.round_ph = None
        else:
            closed = (None, None)
        self.round, self.round_start = r, index0 + 1
        return closed


def reasoned_close(sid: str, t0: float, title: str, r: int,
                   boxes: List[str], labels: List[str], start: int,
                   end: int) -> Tuple[str, Dict[str, Any]]:
    """R9 — (text, keyboard) of ONE reasoning pass's close: its own final
    status with ITS buttons (global slice). The formula the phone
    (live_view._cot_run_close) and the Mac's watcher
    (_edit_reasoned_close) were duplicating — pure, each publishes it over
    its own transport. start/end = 1-based global indexes of its boxes."""
    return (reasoned_text(title, r, end - start,
                          sum(len(b) for b in boxes[start - 1:end - 1])),
            {"inline_keyboard": box_buttons(sid, t0, labels[start - 1:end - 1],
                                            start=start)})


def _persist_cache(sid: str, t0: float, title: str, led: BoxLedger,
                   **extra: Any) -> None:
    """R9 — the watcher's save_cache ALWAYS with up-to-date phs (the
    save_cache+phs+_phs_of+_with_ph quartet that kept repeating). F57:
    ph = the filling box's message or the last frozen one."""
    ph = led.live_ph or (led.box_phs[-1] if led.box_phs else None)
    save_cache(sid, t0, title, **led.cache_kwargs(),
               **_with_ph(ph), **extra)


def _poll_history(c, sid: str, after_id, empty_polls: int):
    """One history fetch of the poll loop. F44 safety net: after
    COT_LIVE_EMPTY_POLLS consecutive polls with NOTHING past after_id
    (warm pickup without even the new prompt in sight) the connection is
    not reflecting the store — a resume (the usual cost, only in this
    anomalous case) re-attaches it. → (msgs, empty_polls)."""
    msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
    if after_id and not msgs:
        empty_polls += 1
        if empty_polls == COT_LIVE_EMPTY_POLLS:
            pwlog("cot-live %s: %d polls with no new history — re-resume"
                  % (sid[-8:], empty_polls))
            c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
    else:
        empty_polls = 0
    return msgs, empty_polls


def _freeze_box(cfg: Dict[str, Any], ph: int, sid: str, t0: float,
                label: str, n: int, box: str) -> None:
    """F57 — the seal: the box's OWN message solidifies with its label
    and ITS single button (global index n). After this edit the message
    is never touched again. F61 verbose: it solidifies ALREADY OPEN
    (box_open_view, ⤴️ to fold back)."""
    if verbosity.born_open(cfg):
        text, kb = box_open_view(sid, t0, n, label, "", box)
    else:
        text = box_done_text(label, len(box))
        kb = {"inline_keyboard": box_buttons(sid, t0, [label], start=n)}
    if uhttp._live_edit(cfg, ph, text, markup=kb):
        kb_log("mac box done %s" % label, ph, kb)


def _live_poll_update(cfg: Dict[str, Any], led: BoxLedger, sid: str,
                      t0: float, title: str,
                      starts: List[Tuple[int, int]],
                      frozen: List[Tuple[int, int]],
                      k: str, pending: int) -> None:
    """F57 per-poll display: freeze the boxes whose live message existed
    (their button appears IN their own message, immediately), advance
    the round numbering (no round messages — the boxes themselves are
    the timeline), and keep the FILLING box's own message alive with
    text-only edits. A frozen message is never edited again."""
    for i0, r in starts:
        led.advance_to(r, i0)
    for i0, ph in frozen:
        _freeze_box(cfg, ph, sid, t0, led.labels[i0], i0 + 1,
                    led.boxes[i0])
    _persist_cache(sid, t0, title, led, live=True)
    if not k:
        return
    if led.live_ph and led.live_label == k:
        if uhttp._live_edit(cfg, led.live_ph,
                            box_live_text(title, k, pending)):
            kb_log("mac box live %s" % k, led.live_ph, None)
    else:
        ph = ph_of(uhttp.send(cfg, box_live_text(title, k, pending),
                              loud=False))
        if ph:
            led.live_birth(ph, k)
            _persist_cache(sid, t0, title, led, live=True)
            kb_log("mac box born %s" % k, ph, None)


def _send_late_boxes(cfg: Dict[str, Any], led: BoxLedger, sid: str,
                     t0: float, title: str,
                     when: Optional[str] = None) -> None:
    """F57 — boxes whose message was never born (429 cooldown, bulk
    arrivals between polls): ONE late message with THOSE buttons — no
    box is unreachable. The F49 guarantee, adapted to per-box ids.
    F72: when — the landing moment (the close, or the re-attach catch-up
    of a muted gap)."""
    late_idx = [i + 1 for i, p in enumerate(led.box_phs) if not p]
    if not late_idx:
        return
    kb = {"inline_keyboard": index_buttons(sid, t0, led.labels, late_idx)}
    ph_late = ph_of(uhttp.send(cfg, late_text(title, len(late_idx), t0,
                                              when=when or "as the turn closed"),
                               loud=False, markup=kb))
    if ph_late:
        for i in late_idx:
            led.box_phs[i - 1] = ph_late
        kb_log("mac late", ph_late, kb)


def _beat_activity(msgs, pending: int) -> Dict[str, Any]:
    """F70 — what the beat line can say RIGHT NOW, from the history:
    the in-flight reasoning's size (live_boxes' pending — it MOVES even
    while the store settles in bulk), the last tool it ran (empty = the
    tail is thinking), and the step count. Pure."""
    tool = ""
    for m in reversed(msgs):
        for p in reversed(m.get("parts") or []):
            if not isinstance(p, dict):
                continue
            if p.get("type") == "tool":
                tool = str(p.get("tool") or "tool")
            break   # only the TAIL part matters: tool = running, else thinking
        if tool:
            break
    steps = sum(1 for m in msgs
                if isinstance(m, dict) and (m.get("info") or {}).get("role")
                == "assistant")
    return {"pending": int(pending), "tool": tool, "steps": max(1, steps)}


def _view_mute(sid) -> bool:
    """F67 — one of this session's agents is on VIEW: the parent's LIVE
    stream holds (picking a subagent means only that subagent streams);
    the closing bundle still travels. Lazy import: agent_live imports
    this module at top level."""
    from zcode_bot import agent_live
    return agent_live.view_alive(sid)


def _ph_seed(sid: str, t0: float) -> Dict[str, int]:
    """F72 — the messages a DEAD watcher left in the chat: a respawned
    watcher (the attach's, after a deploy killed the live one mid-turn)
    starts with an empty ledger and would re-send boxes that are already
    frozen in the chat as a catch-up bundle. The cache the predecessor
    wrote maps each box to its message id — read BEFORE run_watcher's
    startup save_cache wipes it. Done caches seed nothing (their boxes
    are history, not a live turn's)."""
    try:
        with open(cache_path(sid, t0)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or d.get("done") or d.get("finalizado"):
        return {}
    phs = d.get("phs") or []
    return {str(l): p for l, p in zip(d.get("labels") or [], phs) if p}


def _watch_turn(c, cfg, sid, title, t0, start, after_id=None,
                ph_seed=None, catchup=False):
    """ONE Mac turn: polls the history every 3s until the marker
    disappears (on_stop: it closed), another one replaces it (a new
    prompt before we saw it close) or the 35 min ceiling. F57: every box
    lives in its OWN message — born «thinking… CoT R.C» while it fills
    (text-only edits), frozen with its button the moment it seals, never
    touched again; the next box opens a fresh message below.

    after_id: the PREVIOUS turn's last message — this turn's is
    everything after it. In the warm pickup (F44) the new prompt may not
    be persisted on the first poll: without the cutoff, the history's
    last user would be the previous turn's and its boxes would travel
    again.

    → (cut, last): cut None = closed cleanly; (mtime, token) of
    the foreign marker that cut it off; YIELDED if another watcher took
    the pid-file; LIVE_CEILING if 35 min passed with the marker still
    there (it closes with whatever there is and is NOT watched again: an
    orphaned marker would be an eternal loop). last = id of the last
    message seen (the next turn's after_id).

    F72 — ph_seed/catchup: a respawned watcher (the attach's) inherits
    the predecessor's message ids, and lands the boxes the chat never
    saw as ONE collapsed bundle at its FIRST sync; the resume flip (the
    task becoming current again mid-run) does the same for its own muted
    gap — an attach always shows life at once."""
    led = BoxLedger()       # F54d: the turn's boxes/rounds/phs bookkeeping
    seed = dict(ph_seed or {})   # F72: consumed as the labels appear
    seen = -1               # boxes of the last poll DISPLAYED (-1 = none yet:
    #                          the anti-silence birth with 0 boxes needs it)
    live_ones = ([], [])    # the latest shown live (monotonic guard)

    def _ok() -> bool:
        """F71 — every LIVE push of the turn asks: is this session still
        the active task (read live, F15) and not muted by an agent view?
        Before, the gate ran at SPAWN time only: a Mac turn keeps running
        after a task switch and its boxes/beats kept landing over
        whatever the user was doing (26/10: the zcode_bot turn streaming
        over the diff_ask task — the TECH_DEBT note, now closed)."""
        return _task_current_ok(cfg, sid) and not _view_mute(sid)

    last, cut, ceiling, empty_polls = after_id, None, False, 0
    push_ok: Optional[bool] = None   # F72: False→True mid-run = re-attached
    t_end = time.time() + COT_LIVE_MAX_S
    # F51c: a Mac turn that drags on ANNOUNCES itself — same value and
    # same phrase as the worker (R0b); post-box silence read as «it
    # froze». Next announcement in seconds (the _turn_end_poll pattern):
    # patchable in tests and free of granularity surprises.
    next_notice = t0 + config.LONG_TURN_NOTICE_S
    next_beat = t0 + MAC_BEAT_S   # F65b: the while-thinking heartbeat
    # F70 — the beat carries REAL activity (Damian's base requirement:
    # the pipe must look connected and circulating at every moment):
    # the in-flight reasoning's size (it MOVES even while the store
    # settles in bulk), the last tool it ran, the steps. Filled by the
    # poll below; the beat of round N reads round N-1's data.
    act = {"pending": 0, "tool": "", "steps": 0}
    while True:
        if time.time() >= t_end:
            ceiling = _marker_state(sid) is not None
            break
        if _yielded(sid):
            pwlog("cot-live %s: another watcher took the session — "
                  "stepping down" % sid[-8:])
            return YIELDED, last
        pid_beat(sid)
        ok = _ok()   # F71: read LIVE every round; F72: one read, every gate
        if ok and push_ok is False:
            # F72 — re-attached mid-run: the /task pick made this session
            # current again and the watcher kept sealing silently through
            # the muted gap. Show life AT ONCE — the beat this very round
            # and, after the sync below, the gap's boxes as ONE bundle
            next_beat = 0.0
            catchup = True
        push_ok = ok
        if time.time() >= next_notice:
            next_notice = time.time() + max(config.LONG_TURN_NOTICE_S, 60)
            if ok:
                uhttp.send(cfg, "⏱️ «%s» has been running %d min on the Mac — "
                           "still on it (the app's Stop halts it)."
                           % (esc(title), int((time.time() - t0) / 60)),
                           loud=False)
        state = _marker_state(sid)
        if state is None or _other_turn(start, state):
            cut = state
            break
        if time.time() >= next_beat:
            # F65b — the fork's history settles LATE: reasoning parts
            # shrink/land in bulk mid-turn (boxes stall for minutes on a
            # healthy turn). F70 — the beat proves the pipe CIRCULATING:
            # in-flight size (moving), last tool, steps, the pipe stamp.
            # Nothing real to report (a plain text turn) = no line.
            next_beat = time.time() + MAC_BEAT_S
            if ok and (led.boxes or act["pending"] or act["tool"]):
                if act["tool"]:
                    head = "🔧 %s" % esc(act["tool"])
                elif act["pending"] >= 1000:
                    head = "thinking · in-flight ~%dk chars" % (
                        act["pending"] // 1000)
                else:
                    head = "thinking"
                bits = [head, "step %d" % act["steps"],
                        "%d min" % max(1, int((time.time() - t0) / 60))]
                try:
                    from zcode_bot import agent_live  # lazy (import cycle)
                    age = agent_live.last_event_age()
                    if age is not None:
                        bits.append("pipe %ds" % max(0, int(age)))
                except Exception:  # noqa: BLE001 — advisory, never fatal
                    pass
                uhttp.send(cfg, "🧠 «%s» — %s (%d box%s sealed)"
                           % (esc(title), " · ".join(bits), len(led.boxes),
                              "es" if len(led.boxes) != 1 else ""), loud=False)
        if not start[1] and state[1]:
            start = state  # the token arrived whole: from here on it is compared by token
        msgs, empty_polls = _poll_history(c, sid, after_id, empty_polls)
        boxes, labels, pending, k = live_boxes(msgs)
        act = _beat_activity(msgs, pending)   # F70: the beat's real data
        starts, frozen = led.sync(boxes, labels)
        if seed:
            # F72 — the respawned watcher inherits the messages its dead
            # predecessor left frozen in the chat (they are not re-sent)
            for i, l in enumerate(led.labels):
                if led.box_phs[i] is None:
                    led.box_phs[i] = seed.pop(str(l), None)
        if catchup:
            # F72 — the attach's landing, ABOVE the live line: everything
            # the chat never saw (the respawn's first sync, or the boxes
            # of the muted gap) in ONE collapsed bundle — not at the close
            catchup = False
            if _task_current_ok(cfg, sid):   # the close-bundle gate: a live
                _send_late_boxes(cfg, led, sid, t0, title,   # landing never
                                 when="sealed while you were away")  # mutes
        if len(boxes) != seen:
            live_ones = (boxes, labels)
            if ok:
                # F67/F71 muted or switched away: the boxes still seal in
                # the ledger (the cache is always written) — /cot of the
                # task, or its attach, delivers them
                _live_poll_update(cfg, led, sid, t0, title, starts, frozen,
                                  k, pending)
            seen = len(boxes)
        time.sleep(COT_LIVE_POLL_S)
    msgs = []
    if cut is None:
        # closed cleanly (or ceiling): the history already has the whole turn
        msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
        final = turn_boxes(msgs)
        last = _last_id_of(msgs) or last
    else:
        # on_stop → new prompt race: the history already carries the NEXT
        # turn (its user is the last one) — what was shown live is what
        # there is
        final = live_ones
    if len(final[0]) < len(live_ones[0]):
        final = live_ones  # monotonic guard: never less than what was shown
    starts, frozen = led.sync(*final)   # the closing lists replace the slice
    for i0, ph in frozen:    # boxes sealed only in the close-time fetch
        _freeze_box(cfg, ph, sid, t0, led.labels[i0], i0 + 1,
                    led.boxes[i0])
    had_live = led.live_ph is not None
    if led.live_ph and _task_current_ok(cfg, sid):
        # the turn ended mid-fill: the live message closes as a cut line
        # (no sealed box — nothing to tap; the notice of F45 does not
        # apply either: there WAS reasoning in flight)
        uhttp._live_edit(cfg, led.live_ph,
                         box_cut_text(title, led.live_label))
        led.live_ph = None
    if not led.boxes and not had_live and msgs and not ceiling \
            and cfg.get("cot_empty_notice") and _task_current_ok(cfg, sid):
        # F45: closed cleanly, history in plain sight, not one reasoning
        # part and nothing was even filling → say so in one line. With a
        # ceiling or a foreign cut nothing is known: stay silent.
        uhttp.send(cfg, empty_text(title, t0), loud=False)
    if _task_current_ok(cfg, sid):   # F67: the close travels past a view
        _send_late_boxes(cfg, led, sid, t0, title)
    _persist_cache(sid, t0, title, led, done=True)
    pwlog("cot-live end: boxes=%d" % len(led.boxes))
    return (LIVE_CEILING if ceiling else cut), last


def _release(cfg, sid):
    """Reasons for the warm watcher to release the runtime: /stop, the
    task stopped being current, or a bot worker took the session (phone:
    its LiveTurnView already makes the boxes — here they would be
    duplicated)."""
    return _stop_requested(sid) or not _task_current_ok(cfg, sid) \
        or _worker_owned(sid)


def _cot_warm_wait(cfg, sid, timeout_s):
    """F44 — the WARM wait between turns: no runtime, reading small files
    every 1s, until the turn's marker reappears (a new Mac prompt) → its
    (mtime, token). None = release the runtime: keepalive expired,
    _release(), or a successor took the pid-file."""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        if _yielded(sid):
            pwlog("cot-live %s: another watcher took the session — "
                  "stepping down" % sid[-8:])
            return None
        if _release(cfg, sid):
            return None
        state = _marker_state(sid)
        if state is not None:
            return state
        if config.code_changed():
            # F48d: there was a deploy — the hook spawns a new watcher
            # (with the new code) on the Mac's next turn
            pwlog("cot-live %s: new code deployed — stepping down"
                  % sid[-8:])
            return None
        pid_beat(sid)
        time.sleep(COT_LIVE_WAIT_S)
    # last look: the hook that saw us alive an instant ago did NOT spawn
    # another one — a marker that landed right at expiry is just as much
    # ours
    state = _marker_state(sid)
    if state is not None and not _yielded(sid) and not _release(cfg, sid):
        return state
    pwlog("cot-live %s: keepalive expired (%ds) — runtime released"
          % (sid[-8:], timeout_s))
    return None


def run_watcher(cfg: Dict[str, Any], sid: str, title: str, t0: float,
                ws: str, keepalive: Optional[int] = None,
                catchup: bool = False) -> None:
    """F41/F44 — the Mac CoT watcher: ONE resume per SESSION. It watches
    the turn that launched it and, with keepalive, stays WARM waiting for
    the same session's next marker — same connection, no second resume:
    the following turns' first little box arrives in seconds. Each turn's
    cache is written as it is taken: on_stop skips F40's --cot-buttons
    (the watcher is the owner). keepalive 0 = one turn and done (F41).
    F72 catchup — the ATTACH respawn: inherit the dead predecessor's
    message ids and land the chat's unseen boxes at the FIRST sync."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if keepalive is None:
        keepalive = int(cfg.get("cot_live_keepalive_s") or 0)
    start = _marker_state(sid) or (int(t0), "")  # the turn I was sent to watch
    seed = _ph_seed(sid, t0) if catchup else {}   # BEFORE the wipe below
    save_cache(sid, t0, title, [], [], live=True)  # owner: F40 does not duplicate
    pid_write(sid)
    c = None
    try:
        c = zc_proto.make(cwd=ws)
        r = c.request("session/resume", {"sessionId": sid},
                      timeout=COT_RESUME_TIMEOUT_S)
        if e := zc_proto.proto_err(r, "resume"):
            pwlog("cot-live %s: %s" % (sid[-8:], e[:120]))
            save_cache(sid, t0, title, [], [], done=True, error=e[:120])
            return
        last, turns = None, 0
        while True:
            cut, last = _watch_turn(c, cfg, sid, title, t0, start, last,
                                    ph_seed=seed if not turns else None,
                                    catchup=catchup and not turns)
            turns += 1
            if cut is YIELDED or cut is LIVE_CEILING:
                break
            if cut is None:
                if keepalive <= 0:
                    break
                cut = _cot_warm_wait(cfg, sid, keepalive)
                if cut is None:
                    break
            # F44: new turn of the SAME session, same connection. The title
            # is recalculated the way the hook would on spawn (the tasks
            # index, or the workspace folder)
            verbosity.refresh(cfg)  # F61: /verbosity may have changed since
            start, t0 = cut, cut[0]
            title = task_label({"session_id": sid, "cwd": ws}) or title
            save_cache(sid, t0, title, [], [], live=True)  # possession: F40 does not duplicate
        if turns > 1:
            pwlog("cot-live %s: %d turns with a single resume" % (sid[-8:], turns))
    finally:
        pid_remove(sid)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def cmd_cot_live(sid: str, title: str, t0: str, ws: str,
                 catchup: bool = False) -> None:
    """Entrypoint of the detached subprocess (gateway --cot-live).
    F72: catchup — spawned by the ATTACH (mid-turn respawn)."""
    cfg = load_config()
    try:
        run_watcher(cfg, sid, title, float(t0), ws, catchup=catchup)
    except Exception as exc:  # noqa: BLE001 - the subprocess reports, it does not die
        pwlog("cot-live %s failed: %s" % (sid[-8:], exc))


def cmd_cot_buttons(sid: str, title: str, t0: str, ws: str) -> None:
    """Entrypoint of the detached subprocess (gateway --cot-buttons)."""
    cfg = load_config()
    try:
        k = capture(cfg, sid, title, float(t0), ws)
        pwlog("cot-buttons %s: %s boxes" % (sid[-8:], k))
    except Exception as exc:  # noqa: BLE001 - the subprocess reports, it does not die
        pwlog("cot-buttons %s failed: %s" % (sid[-8:], exc))
