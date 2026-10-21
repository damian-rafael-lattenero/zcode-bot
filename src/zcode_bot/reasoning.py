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

from zcode_bot import config
from zcode_bot.config import marker_path
from zcode_bot.core import _stop_requested, _task_current_ok, _worker_owned
from zcode_bot import proto as zc_proto
from zcode_bot.messages import _fetch_messages, _last_id_of
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc, equal_parts, task_label
from zcode_bot.utils.config import load_config
from zcode_bot.utils.logging import pwlog

COT_PART_CAP = 3000      # chars per part when OPENING an old/large box
COT_BOX_CAP = COT_PART_CAP  # chars per BOX: at the cap it is sealed and its button travels
COT_BUTTONS_MAX = 20     # keyboard: more boxes → /cot note (document)
COT_BUTTONS_ROW = 5
COT_MSGS = 60            # messages to fetch: the longest Mac turn fits
COT_RESUME_TIMEOUT_S = 120
COT_CACHE_FMT = "cot-%s-%d.json"
COT_PID_FMT = "cot-live-%s.pid"  # F44: pid of the session's warm watcher
# P6: the pid-file name and the cache keys below were Spanish until P6
# (cot-vivo-*.pid; titulo/cajas/rotulos/vivo/finalizado/bloques). Writes  # noqa: spanish — old cache shape, readers stay bilingual
# are English-only since then; the READERS stay bilingual forever — a
# 24h-old Telegram button must still open its box (the on-disk caches it
# points at were written by the pre-deploy code).
_PID_PREFIXES = ("cot-live-", "cot-vivo-")  # dual glob: old files age out


def cache_path(sid, t0):
    return os.path.join(config.STATE_DIR, COT_CACHE_FMT % (sid, int(t0)))


def ph_of(res):
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

    def __init__(self, cap=COT_BOX_CAP):
        self.cap = cap
        self.text = ""
        self.sealed = 0

    def feed(self, delta):
        """[(box, n_local)] of the boxes that reached the cap."""
        self.text += delta
        filled = []
        while len(self.text) >= self.cap * (self.sealed + 1):
            self.sealed += 1
            cut = self.cap * self.sealed
            filled.append((self.text[cut - self.cap:cut],
                           self.sealed))
        return filled

    def close(self):
        """End of the reasoning pass: the tail (if it has anything) is
        the last box. State resets for the next pass."""
        tail = self.text[self.cap * self.sealed:]
        n = self.sealed + 1
        self.text, self.sealed = "", 0
        return [(tail, n)] if tail.strip() else []

    @property
    def in_progress(self):
        """(chars of the box currently filling, that box's n_local)."""
        rest = len(self.text) - self.cap * self.sealed
        return max(0, rest), self.sealed + 1


def text_boxes(text, cap=COT_BOX_CAP):
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


def turn_boxes(msgs):
    """COMPLETE boxes of the turn (turn already closed): each reasoning
    pass fills its boxes to the cap + a tail. F46: label «CoT R.C»
    (pass R, box C) — unique, never two «CoT 1». → parallel
    (boxes, labels)."""
    reasoned, _in_progress = _turn_parts(msgs)
    return _boxes_from_reasoned(reasoned)


def live_boxes(msgs):
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


def box_buttons(sid, t0, labels, start=1):
    """One button per box; labels «CoT R.C» (F46: never two «CoT 1») and
    the callback_data carries the GLOBAL index: ct:sid:t0:n. start = the
    first label's global index (F46: ONE pass's keyboard is a slice of
    the total)."""
    return _rows([{"text": labels[i - start][:36],
                   "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                  for i in range(start,
                                 start + min(len(labels), COT_BUTTONS_MAX))])


def part_buttons(sid, t0, n, total, current):
    return _rows([{"text": ("[%d/%d]" if p == current else "%d/%d")
                   % (p, total),
                   "callback_data": "ct:%s:%d:%d:%d" % (sid, int(t0), n, p)}
                  for p in range(1, total + 1)])


def index_buttons(sid, t0, labels, idxs):
    """F49 — buttons for arbitrary 1-based GLOBAL indexes (the boxes that
    live in ONE message: the pass's own, or the late ones from the
    close); labels is the turn's complete list."""
    return _rows([{"text": labels[i - 1][:36],
                   "callback_data": "ct:%s:%d:%d" % (sid, int(t0), i)}
                  for i in idxs[:COT_BUTTONS_MAX]])


def kb_log(source, ph, kb):
    """F49 — the feedback that was missing (Damian: «you have no way to
    get feedback»): every send/edit of a button box records in the pwlog
    WHICH buttons that message ended up showing. So the chat can be
    rebuilt from the log without looking at it."""
    tags = [b.get("text", "?") for row in (kb or {}).get(
        "inline_keyboard", []) for b in row]
    pwlog("cot kb %s ph=%s [%d]: %s" % (source, ph, len(tags),
                                        " ".join(tags)))


# ------------------------------------------------------------- cache

def save_cache(sid, t0, title, boxes, labels, **extra):
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


def cache_exists(sid, t0):
    return os.path.exists(cache_path(sid, t0))


def cache_closed_or_live(sid, t0):
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


def close_cache(sid, t0, **extra):
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


def live_text(title, n, in_progress="", pending=0):
    """Status of the button box: sealed boxes + the one filling
    (in_progress = «R.C» label of the box being filled)."""
    line = "🧠 <b>«%s»</b> thinking… · %d box%s" % (
        esc(title or "?"), n, "es" if n != 1 else "")
    if pending:
        line += " · filling CoT %s (%d chars)" % (in_progress, pending)
    if n:
        line += "\nTap one to read it:"
    return line


def reasoned_text(title, r, n, total):
    """F46 — close of ONE reasoning pass: its button box keeps its own
    final status (the turn goes on: the next pass is born in a new
    message, in its chronological position)."""
    return ("🧠 <b>«%s»</b> · pass %d — %d box%s, %d chars. "
            "Tap one to read it:"
            % (esc(title or "?"), r, n, "es" if n != 1 else "", total))


def final_text(title, t0, n, total):
    hhmm = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    text = ("🧠 <b>Reasoning</b> of «%s» (turn at %s) — %d box%s, "
            "%d chars. Tap one to read it:"
            % (esc(title or "?"), hhmm, n, "es" if n != 1 else "", total))
    if n > COT_BUTTONS_MAX:
        text += ("\n(buttons for the first %d; the full document "
                 "with /cot)" % COT_BUTTONS_MAX)
    return text


def late_text(title, n, t0):
    """F49 — boxes whose reasoning pass never got its own message (send
    failed on the phone; passes born in the Mac's close-time fetch): ONE
    message at the end with THOSE — no button is lost and nothing piles
    up (the old global index: «they all piled up in the last box»).
    F51a: 📥 + «turn of HH:MM» — it lands at the close and without an
    anchor it read as another turn's activity (incident 21/10 08:18: the
    07:43 turn's boxes falling onto a just-started turn)."""
    hhmm = datetime.datetime.fromtimestamp(float(t0)).strftime("%H:%M")
    return ("📥 🧠 <b>«%s»</b> · %d box%s as the turn closed — turn at "
            "%s. Tap one to read it:"
            % (esc(title or "?"), n, "es" if n != 1 else "", hhmm))


def empty_text(title, t0):
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

def capture(cfg, sid, title, t0, ws):
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


def show(sid, t0, n, part=1):
    """The tap: box n from the cache (local label; global index on the button)."""
    try:
        with open(cache_path(sid, t0)) as f:
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


def pid_path(sid):
    return os.path.join(config.STATE_DIR, COT_PID_FMT % _safe_sid(sid))


def pid_write(sid):
    """The watcher registers itself: its pid in the file, its heartbeat
    in the mtime. The hook reads it to avoid spawning a second
    watcher."""
    with open(pid_path(sid), "w") as f:
        f.write(str(os.getpid()))


def pid_beat(sid):
    try:
        os.utime(pid_path(sid), None)
    except OSError:
        pass


def pid_read(sid):
    for name in (COT_PID_FMT % _safe_sid(sid),
                 "cot-vivo-%s.pid" % _safe_sid(sid)):  # pre-F53 watcher
        try:
            with open(os.path.join(config.STATE_DIR, name)) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            continue
    return 0


def pid_remove(sid):
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


def watcher_alive(sid, proc_alive=None, now=None, fresh_s=COT_LIVE_HB_FRESH_S):
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


def watcher_pids():
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


def watchers_cleanup():
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


def reasoned_close(sid, t0, title, r, boxes, labels, start, end):
    """R9 — (text, keyboard) of ONE reasoning pass's close: its own final
    status with ITS buttons (global slice). The formula the phone
    (live_view._cot_run_close) and the Mac's watcher
    (_edit_reasoned_close) were duplicating — pure, each publishes it over
    its own transport. start/end = 1-based global indexes of its boxes."""
    return (reasoned_text(title, r, end - start,
                          sum(len(b) for b in boxes[start - 1:end - 1])),
            {"inline_keyboard": box_buttons(sid, t0, labels[start - 1:end - 1],
                                            start=start)})


def _edit_reasoned_close(cfg, ph, sid, t0, title, r, boxes, labels, start, end):
    """F46 — close of ONE reasoning pass's button box (the Mac's
    watcher): its own final status with ITS buttons; the next pass is
    born in a new message (chronological position: little boxes of phase
    1, tools/diffs, little boxes of phase 2…)."""
    text, kb = reasoned_close(sid, int(t0), title, r, boxes, labels,
                              start, end)
    try:
        if uhttp._live_edit(cfg, ph, text, markup=kb):
            kb_log("mac close r%d" % r, ph, kb)
    except Exception:  # noqa: BLE001 — the close must never break the watcher
        pass


def _watch_turn(c, cfg, sid, title, t0, start, after_id=None):
    """ONE Mac turn: polls the history every 3s until the marker
    disappears (on_stop: it closed), another one replaces it (a new
    prompt before we saw it close) or the 35 min ceiling. Each box that
    fills adds its button; the button box is BORN with the first one.

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
    message seen (the next turn's after_id)."""
    seen, ph = -1, None
    phs_map = {}             # F46: pass → message_id of ITS button box
    r_current, ph_start = 0, 1  # pass on screen and its first global index
    live_ones = ([], [])     # the latest shown live (monotonic guard)

    def _persist(**extra):
        """R9 — the watcher's save_cache ALWAYS with up-to-date phs (the
        save_cache+phs+_phs_of+_with_ph quartet that kept repeating)."""
        save_cache(sid, t0, title, boxes, labels,
                   phs=_phs_of(labels, phs_map), **_with_ph(ph), **extra)
    last, cut, ceiling, empty_polls = after_id, None, False, 0
    t_end = time.time() + COT_LIVE_MAX_S
    # F51c: a Mac turn that drags on ANNOUNCES itself — same value and
    # same phrase as the worker (R0b); post-box silence read as «it
    # froze». Next announcement in seconds (the _turn_end_poll pattern):
    # patchable in tests and free of granularity surprises.
    next_notice = t0 + config.LONG_TURN_NOTICE_S
    while True:
        if time.time() >= t_end:
            ceiling = _marker_state(sid) is not None
            break
        if _yielded(sid):
            pwlog("cot-live %s: another watcher took the session — "
                  "stepping down" % sid[-8:])
            return YIELDED, last
        pid_beat(sid)
        if time.time() >= next_notice:
            next_notice = time.time() + max(config.LONG_TURN_NOTICE_S, 60)
            uhttp.send(cfg, "⏱️ «%s» has been running %d min on the Mac — "
                       "still on it (the app's Stop halts it)."
                       % (esc(title), int((time.time() - t0) / 60)),
                       loud=False)
        state = _marker_state(sid)
        if state is None or _other_turn(start, state):
            cut = state
            break
        if not start[1] and state[1]:
            start = state  # the token arrived whole: from here on it is compared by token
        msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
        if after_id and not msgs:
            empty_polls += 1
            if empty_polls == COT_LIVE_EMPTY_POLLS:
                # warm pickup without even the new prompt in sight: the
                # connection is not reflecting the store — a resume (the
                # usual cost, only in this anomalous case) hooks it back up
                pwlog("cot-live %s: %d polls with no new history — re-resume"
                      % (sid[-8:], empty_polls))
                c.request("session/resume", {"sessionId": sid},
                          timeout=COT_RESUME_TIMEOUT_S)
        else:
            empty_polls = 0
        boxes, labels, pending, k = live_boxes(msgs)
        if len(boxes) != seen:
            live_ones = (boxes, labels)
            # F46: one button box PER PASS — the first is born right away
            # (anti-silence while resuming), the others with their first
            # box sealed; the previous one stays closed with its own final
            # status.
            for i in range(max(seen, 0), len(boxes)):
                r = _parse_reasoned_label(labels[i]) if i < len(labels) else 0
                if r and r != r_current:
                    if ph and r_current:
                        phs_map[r_current] = ph
                        _edit_reasoned_close(cfg, ph, sid, t0, title,
                                             r_current, boxes, labels,
                                             ph_start, i + 1)
                        ph = None
                    # F49: the anti-silence message (r_current=0, born with
                    # 0 boxes) is ADOPTED as the first pass's — before, it
                    # closed as «pass 0 — 0 boxes» and the pass was born in
                    # another message: an empty stub above every turn that
                    # started mid-thought
                    r_current, ph_start = r, i + 1
            text = live_text(title, len(boxes) - ph_start + 1, k, pending)
            kb = {"inline_keyboard": box_buttons(
                sid, t0, labels[ph_start - 1:], start=ph_start)}
            _persist(live=True)
            if ph:
                if uhttp._live_edit(cfg, ph, text, markup=kb):
                    kb_log("mac live r%d" % r_current, ph, kb)
            else:
                ph = ph_of(uhttp.send(cfg, text, loud=False, markup=kb))
                if ph:
                    phs_map[r_current] = ph
                    _persist(live=True)
                    kb_log("mac born r%d" % r_current, ph, kb)
            seen = len(boxes)
        time.sleep(COT_LIVE_POLL_S)
    msgs = []
    if cut is None:
        # closed cleanly (or ceiling): the history already has the whole turn
        msgs = _fetch_messages(c, sid, after_id=after_id, limit=COT_MSGS) or []
        boxes, labels = turn_boxes(msgs)
        last = _last_id_of(msgs) or last
    else:
        # on_stop → new prompt race: the history already carries the NEXT
        # turn (its user is the last one) — what was shown live is what
        # there is
        boxes, labels = live_ones
    if len(boxes) < len(live_ones[0]):
        boxes, labels = live_ones  # monotonic guard: never less than what was shown
    if ph:
        phs_map[r_current] = ph
    if not boxes and msgs and not ceiling and cfg.get("cot_empty_notice"):
        # F45: closed cleanly, history in plain sight and not one
        # reasoning part → say so: in the live box if it was born (before
        # it stayed at «0 boxes, 0 chars. Tap one to read it» with no
        # buttons), otherwise in one line. With a ceiling or a foreign cut
        # nothing is known: stay silent.
        if ph:
            uhttp._live_edit(cfg, ph, empty_text(title, t0))
        else:
            uhttp.send(cfg, empty_text(title, t0), loud=False)
    elif ph:
        # F49: the pass on screen closes like all the others — ITS
        # message, ITS buttons. Before, the last message turned into an
        # index with ALL the turn's boxes (Damian: «they all piled up in
        # the last box, it is running really badly»).
        idx = [i + 1 for i, l in enumerate(labels)
               if _parse_reasoned_label(l) == r_current]
        if idx:
            _edit_reasoned_close(cfg, ph, sid, t0, title, r_current, boxes,
                                 labels, idx[0], idx[-1] + 1)
        else:
            # the message on screen has no boxes of its own: it was born
            # anti-silence and the turn did not reason, another prompt cut
            # it, or the boxes only arrived in this fetch. A single pass →
            # the message IS its own; several (or none) → the usual close,
            # and if there are boxes they all live in THAT message
            rs = sorted({_parse_reasoned_label(l) for l in labels})
            if len(rs) == 1 and rs[0]:
                phs_map[rs[0]] = ph
                _edit_reasoned_close(cfg, ph, sid, t0, title, rs[0], boxes,
                                     labels, 1, len(labels) + 1)
            else:
                kb = {"inline_keyboard": box_buttons(sid, t0, labels)}
                if uhttp._live_edit(cfg, ph, final_text(
                        title, t0, len(boxes), sum(len(b) for b in boxes)),
                        markup=kb):
                    for l in labels:
                        phs_map[_parse_reasoned_label(l)] = ph
                    if labels:
                        kb_log("mac single", ph, kb)
    elif boxes:
        # nothing was born live (the boxes only arrived in this fetch):
        # the usual close, ONE message with all of them
        kb = {"inline_keyboard": box_buttons(sid, t0, labels)}
        ph = ph_of(uhttp.send(cfg, final_text(
            title, t0, len(boxes), sum(len(b) for b in boxes)),
            loud=False, markup=kb))
        if ph:
            for l in labels:
                phs_map[_parse_reasoned_label(l)] = ph
            kb_log("mac single", ph, kb)
    # F49: passes without their own message (born in the close-time
    # fetch): ONE late-boxes message with THOSE boxes — no button is lost
    # and nothing piles up in another pass's message
    late_idx = [i + 1 for i, p in enumerate(_phs_of(labels, phs_map)) if not p]
    if late_idx:
        kb = {"inline_keyboard": index_buttons(sid, t0, labels, late_idx)}
        ph_late = ph_of(uhttp.send(cfg, late_text(title, len(late_idx), t0),
                                   loud=False, markup=kb))
        if ph_late:
            for i in late_idx:
                phs_map[_parse_reasoned_label(labels[i - 1])] = ph_late
            kb_log("mac late", ph_late, kb)
    _persist(done=True)
    pwlog("cot-live end: boxes=%d" % len(boxes))
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


def run_watcher(cfg, sid, title, t0, ws, keepalive=None):
    """F41/F44 — the Mac CoT watcher: ONE resume per SESSION. It watches
    the turn that launched it and, with keepalive, stays WARM waiting for
    the same session's next marker — same connection, no second resume:
    the following turns' first little box arrives in seconds. Each turn's
    cache is written as it is taken: on_stop skips F40's --cot-buttons
    (the watcher is the owner). keepalive 0 = one turn and done (F41)."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if keepalive is None:
        keepalive = int(cfg.get("cot_live_keepalive_s") or 0)
    start = _marker_state(sid) or (int(t0), "")  # the turn I was sent to watch
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
            cut, last = _watch_turn(c, cfg, sid, title, t0, start, last)
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


def cmd_cot_live(sid, title, t0, ws):
    """Entrypoint of the detached subprocess (gateway --cot-live)."""
    cfg = load_config()
    try:
        run_watcher(cfg, sid, title, float(t0), ws)
    except Exception as exc:  # noqa: BLE001 - the subprocess reports, it does not die
        pwlog("cot-live %s failed: %s" % (sid[-8:], exc))


def cmd_cot_buttons(sid, title, t0, ws):
    """Entrypoint of the detached subprocess (gateway --cot-buttons)."""
    cfg = load_config()
    try:
        k = capture(cfg, sid, title, float(t0), ws)
        pwlog("cot-buttons %s: %s boxes" % (sid[-8:], k))
    except Exception as exc:  # noqa: BLE001 - the subprocess reports, it does not die
        pwlog("cot-buttons %s failed: %s" % (sid[-8:], exc))
