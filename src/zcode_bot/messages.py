"""messages — parsers for ProtoClient messages (F4c).

PURE functions over session/messages responses: text/reasoning
extraction, tool call inventory and tool HTML boxes. No network I/O or
state — the ProtoClient `c` comes in as a parameter.
"""
import difflib
import re
import json
from typing import NamedTuple

from zcode_bot import limits
from zcode_bot.utils.formatting import (esc, preview, html_clip,
                                         lines_stamp)


class ToolStep(NamedTuple):
    """A turn's tool call for the step index (live box, trace).
    NamedTuple because the historical shape was the (name, desc)
    tuple: unpackings stay intact."""
    name: str
    desc: str


def _extract_assistant_text(msgs, last_only=False):
    """Text of the assistant messages: ONLY parts with type=="text" (no
    reasoning/tool noise). Real structure:
    {info:{role,id}, parts:[{type,text}]}."""
    ass = [m for m in (msgs if isinstance(msgs, list) else [])
           if isinstance(m, dict) and (m.get("info") or {}).get("role") == "assistant"]
    if last_only and ass:
        ass = ass[-1:]
    out = []
    for m in ass:
        for part in m.get("parts") or []:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                out.append(part["text"])
    return "\n\n".join(t for t in out if t.strip())


def _fetch_messages(c, sid, after_id=None, limit=40):
    r = c.request("session/messages",
                  {"sessionId": sid, "limit": limit, **({"afterMessageId": after_id} if after_id else {})},
                  timeout=60)
    if "error" in r:
        return None
    res = r.get("result")
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("messages", "items", "data"):
            if isinstance(res.get(k), list):
                return res[k]
    return []


def _last_id_of(msgs):
    for m in reversed(msgs if isinstance(msgs, list) else []):
        if isinstance(m, dict):
            info = m.get("info") or {}
            mid = info.get("id") or info.get("messageId") or m.get("messageID")
            if mid:
                return mid
    return None


def _steps_of_messages(msgs):
    """[ToolStep(name, desc)] of the tool calls in a message list
    (parts type=='tool': state.input carries command/file_path).
    Shape-tolerant."""
    return [ToolStep(n, _tool_short_desc(n, inp))
            for n, inp in _tools_of_messages(msgs)]


def _tools_with_call_id(msgs):
    """F7c — [(callID, name, input_dict, status)] of the tool calls.
    The callID (part.callID) pairs the historical tool with the stream's
    toolCallId events (spike F7a: same key on both sides)."""
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        if not isinstance(m, dict):
            continue
        for p in m.get("parts") or []:
            if not (isinstance(p, dict) and p.get("type") == "tool"):
                continue
            st = p.get("state") or {}
            name = str(p.get("tool") or p.get("name") or st.get("name")
                       or "tool")
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            out.append((p.get("callID") or p.get("callId"), name, inp,
                        st.get("status")))
    return out


def _tools_of_messages(msgs):
    """F22bis — the COMPLETE [(name, input_dict)] of tool calls (the
    input feeds the permanent boxes: full command, edit old/new, write
    content). Shape-tolerant."""
    return [(n, inp) for _cid, n, inp, _st in _tools_with_call_id(msgs)]


def _tool_short_desc(name, inp):
    """One-line desc (step index, live box, trace)."""
    if isinstance(inp.get("command"), list):
        return " ".join(str(x) for x in inp["command"])[:90]
    if isinstance(inp.get("command"), str):
        return inp["command"][:90]
    for k in ("file_path", "path"):
        if inp.get(k):
            return str(inp[k])
    if inp.get("pattern"):
        return "pattern " + str(inp["pattern"])[:60]
    return ""


def colorize_diff(lines, hunks_sep=False):
    """Lines of a unified diff (WITHOUT the ---/+++ headers) → lines for
    the phone: 🟢 added / 🔴 removed / · context. Telegram's <pre>
    doesn't support color — the emoji encodes each line (Damian's
    request: see it green/red, one under the other). First line =
    diffstat ● +N −M. ONE renderer for both sources (F38d): the live
    box (difflib over the Edit's old/new_string) and /diff (git output).
    The @@ lines are dropped in boxes (noise in small chats) or become
    '⋯ line N' with hunks_sep — in a whole file the jumps between
    hunks do matter."""
    more = sum(1 for ln in lines if ln.startswith("+"))
    less = sum(1 for ln in lines if ln.startswith("-"))
    out = []
    if more or less:
        out.append("● 🟢 +%d  🔴 −%d" % (more, less))
    for ln in lines:
        if ln.startswith("+"):
            out.append("🟢 " + ln)
        elif ln.startswith("-"):
            out.append("🔴 " + ln)
        elif ln.startswith("@@"):
            if hunks_sep:
                m = re.search(r"\+(\d+)", ln)
                out.append("⋯ line %s" % (m.group(1) if m else "?"))
        else:
            out.append("· " + ln)
    return out


def _colored_diff(old, new, max_chars=3400):
    """Live box of the Edit tool: difflib over old/new_string → colorize_diff."""
    d = list(difflib.unified_diff(old, new, lineterm=""))[2:]
    return "\n".join(colorize_diff(d))[:max_chars] or "(no changes)"


# box-filter presets (config tools_box_filter — F7c+)
_IDX_ONLY = {"Read", "View", "Grep", "Glob", "LS", "TodoWrite", "TodoRead",
             "AskUserQuestion", "Skill", "ListModels", "ListMCPResources",
             "Task", "TaskOutput", "TaskStop"}
_MINIMAL = {"Bash", "Edit", "MultiEdit", "Replace", "Write", "Create", "New",
            "NotebookEdit"}


def _tool_box_on(name, cfg):
    """Does this tool get its own PERMANENT box? Presets via config
    tools_box_filter: 'all' | 'action' (default — everything that DOES
    something: bash, edits, subagents, MCP, web) | 'minimal' (bash+edits).
    Tools without a box stay visible: step index + live box."""
    mode = (cfg or {}).get("tools_box_filter") or "action"  # cfg may be {}
    if mode == "all":
        return True
    if mode == "minimal":
        return name in _MINIMAL
    return name not in _IDX_ONLY


def _tool_box(name, inp):
    """F22bis — text of a tool call's PERMANENT box: the COMPLETE command
    (Damian's request), real diff for edits/writes. 3400-char cap
    (Telegram 4096 - formatting). Returns HTML."""
    desc = _tool_short_desc(name, inp)
    head = "🔧 <b>%s</b>%s" % (esc(name),
                               (" — <code>%s</code>" % esc(preview(desc, 120)))
                               if desc else "")
    body = ""
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    if isinstance(cmd, str) and cmd.strip():
        # (audit 20/10) size cap measured on the ESCAPED text: raw caps
        # inflated 5x with esc() and the box arrived cut mid-command by
        # send()'s truncation
        body = "<pre>%s</pre>" % esc(html_clip(cmd, limits.CH_BOX))
        out = inp.get("output") or inp.get("result") or ""
        if isinstance(out, str) and out.strip():
            body += "\n└ <pre>%s</pre>" % esc(html_clip(out, 800))
    elif isinstance(inp.get("edits"), list) and inp["edits"]:
        # MultiEdit: ONE colored diff per edit (before it fell to the
        # raw-JSON fallback — «lo vi todo junto y no se entendió»)  # noqa: spanish — Damian's quoted remark
        chunks = []
        for i, ed in enumerate(inp["edits"], 1):
            if not isinstance(ed, dict):
                continue
            chunks.append("· edit %d\n%s" % (
                i, _colored_diff(str(ed.get("old_string") or "")
                                 .splitlines(),
                                 str(ed.get("new_string") or "")
                                 .splitlines(), limits.CH_BOX // max(1, len(inp["edits"])))))
        body = "<pre>%s</pre>" % esc(
            html_clip("\n".join(chunks), limits.CH_BOX))
    elif name in ("Edit", "MultiEdit", "Replace") and inp.get("old_string") is not None:
        body = "<pre>%s</pre>" % esc(html_clip(_colored_diff(
            str(inp.get("old_string") or "").splitlines(),
            str(inp.get("new_string") or "").splitlines(), limits.CH_BOX),
            limits.CH_BOX))
    elif name in ("Write", "Create", "New") and inp.get("content"):
        lines = str(inp["content"]).splitlines()
        # F50b: the line-count stamp on EVERY file — a Write is all new:
        # green dot + the file's lines
        body = ("<pre>%s</pre>\n<i>%s</i>"
                % (esc(html_clip(
                    "\n".join("🟢 " + ln for ln in lines[:40]),
                    limits.CH_BOX)),
                   lines_stamp(len(lines), 0, len(lines))))
    elif name in ("Read", "View"):
        body = "<i>reading</i> <code>%s</code>" % esc(preview(desc, 160))
    else:
        try:
            body = "<pre>%s</pre>" % esc(html_clip(
                json.dumps(inp, ensure_ascii=False), limits.CH_BOX))
        except (TypeError, ValueError):
            body = ""
    return head + ("\n" + body if body else "")


def _reasoning_of_messages(msgs):
    """Complete and IN-ORDER reasoning of the assistant messages
    (parts type=='reasoning' — the CoT stays persisted in history)."""
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        if not (isinstance(m, dict) and (m.get("info") or {}).get("role") == "assistant"):
            continue
        for p in m.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "reasoning":
                t = p.get("text") or p.get("reasoning") or ""
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
    return "\n\n".join(out)
