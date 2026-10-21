"""messages — parsers for ProtoClient messages (F4c).

PURE functions over session/messages responses: text/reasoning
extraction, tool call inventory and tool HTML boxes. No network I/O or
state — the ProtoClient `c` comes in as a parameter.
"""
import difflib
import re
import json
from typing import NamedTuple

from zcode_bot import limites as lim
from zcode_bot.utils.formatting import (esc, preview, recorte_html,
                                         sello_lineas)


class ToolStep(NamedTuple):
    """A turn's tool call for the step index (live box, trace).
    NamedTuple because the historical shape was the (nombre, desc)
    tuple: unpackings stay intact."""
    nombre: str
    desc: str


def _extract_assistant_text(msgs, solo_ultima=False):
    """Text of the assistant messages: ONLY parts with type=="text" (no
    reasoning/tool noise). Real structure:
    {info:{role,id}, parts:[{type,text}]}."""
    ass = [m for m in (msgs if isinstance(msgs, list) else [])
           if isinstance(m, dict) and (m.get("info") or {}).get("role") == "assistant"]
    if solo_ultima and ass:
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
    """[ToolStep(nombre, desc)] of the tool calls in a message list
    (parts type=='tool': state.input carries command/file_path).
    Shape-tolerant."""
    return [ToolStep(n, _tool_desc_corto(n, inp))
            for n, inp in _tools_of_messages(msgs)]


def _tools_con_call_id(msgs):
    """F7c — [(callID, nombre, input_dict, status)] of the tool calls.
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
            nombre = str(p.get("tool") or p.get("name") or st.get("name")
                         or "tool")
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            out.append((p.get("callID") or p.get("callId"), nombre, inp,
                        st.get("status")))
    return out


def _tools_of_messages(msgs):
    """F22bis — the COMPLETE [(nombre, input_dict)] of tool calls (the
    input feeds the permanent boxes: full command, edit old/new, write
    content). Shape-tolerant."""
    return [(n, inp) for _cid, n, inp, _st in _tools_con_call_id(msgs)]


def _tool_desc_corto(nombre, inp):
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


def colorear_diff(lineas, hunks_sep=False):
    """Lines of a unified diff (WITHOUT the ---/+++ headers) → lines for
    the phone: 🟢 added / 🔴 removed / · context. Telegram's <pre>
    doesn't support color — the emoji encodes each line (Damian's
    request: see it green/red, one under the other). First line =
    diffstat ● +N −M. ONE renderer for both sources (F38d): the live
    box (difflib over the Edit's old/new_string) and /diff (git output).
    The @@ lines are dropped in boxes (noise in small chats) or become
    '⋯ línea N' with hunks_sep — in a whole file the jumps between
    hunks do matter."""
    mas = sum(1 for ln in lineas if ln.startswith("+"))
    menos = sum(1 for ln in lineas if ln.startswith("-"))
    out = []
    if mas or menos:
        out.append("● 🟢 +%d  🔴 −%d" % (mas, menos))
    for ln in lineas:
        if ln.startswith("+"):
            out.append("🟢 " + ln)
        elif ln.startswith("-"):
            out.append("🔴 " + ln)
        elif ln.startswith("@@"):
            if hunks_sep:
                m = re.search(r"\+(\d+)", ln)
                out.append("⋯ línea %s" % (m.group(1) if m else "?"))
        else:
            out.append("· " + ln)
    return out


def _diff_coloreado(viejo, nuevo, max_chars=3400):
    """Live box of the Edit tool: difflib over old/new_string → colorear_diff."""
    d = list(difflib.unified_diff(viejo, nuevo, lineterm=""))[2:]
    return "\n".join(colorear_diff(d))[:max_chars] or "(sin cambios)"


# box-filter presets (config tools_caja_filtro — F7c+)
_IDX_SOLO = {"Read", "View", "Grep", "Glob", "LS", "TodoWrite", "TodoRead",
             "AskUserQuestion", "Skill", "ListModels", "ListMCPResources",
             "Task", "TaskOutput", "TaskStop"}
_MINIMO = {"Bash", "Edit", "MultiEdit", "Replace", "Write", "Create", "New",
           "NotebookEdit"}


def _tool_caja_on(nombre, cfg):
    """Does this tool get its own PERMANENT box? Presets via config
    tools_caja_filtro: 'todas' | 'accion' (default — everything that DOES
    something: bash, edits, subagents, MCP, web) | 'minimo' (bash+edits).
    Tools without a box stay visible: step index + live box."""
    modo = (cfg or {}).get("tools_caja_filtro") or "accion"  # cfg may be {}
    if modo == "todas":
        return True
    if modo == "minimo":
        return nombre in _MINIMO
    return nombre not in _IDX_SOLO


def _tool_caja(nombre, inp):
    """F22bis — text of a tool call's PERMANENT box: the COMPLETE command
    (Damian's request), real diff for edits/writes. 3400-char cap
    (Telegram 4096 - formatting). Returns HTML."""
    desc = _tool_desc_corto(nombre, inp)
    head = "🔧 <b>%s</b>%s" % (esc(nombre),
                               (" — <code>%s</code>" % esc(preview(desc, 120)))
                               if desc else "")
    cuerpo = ""
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    if isinstance(cmd, str) and cmd.strip():
        # (audit 20/10) size cap measured on the ESCAPED text: raw caps
        # inflated 5x with esc() and the box arrived cut mid-command by
        # send()'s truncation
        cuerpo = "<pre>%s</pre>" % esc(recorte_html(cmd, lim.CH_CAJA))
        sal = inp.get("output") or inp.get("result") or ""
        if isinstance(sal, str) and sal.strip():
            cuerpo += "\n└ <pre>%s</pre>" % esc(recorte_html(sal, 800))
    elif isinstance(inp.get("edits"), list) and inp["edits"]:
        # MultiEdit: ONE colored diff per edit (before it fell to the
        # raw-JSON fallback — «lo vi todo junto y no se entendió»)
        trozos = []
        for i, ed in enumerate(inp["edits"], 1):
            if not isinstance(ed, dict):
                continue
            trozos.append("· edit %d\n%s" % (
                i, _diff_coloreado(str(ed.get("old_string") or "")
                                   .splitlines(),
                                   str(ed.get("new_string") or "")
                                   .splitlines(), lim.CH_CAJA // max(1, len(inp["edits"])))))
        cuerpo = "<pre>%s</pre>" % esc(
            recorte_html("\n".join(trozos), lim.CH_CAJA))
    elif nombre in ("Edit", "MultiEdit", "Replace") and inp.get("old_string") is not None:
        cuerpo = "<pre>%s</pre>" % esc(recorte_html(_diff_coloreado(
            str(inp.get("old_string") or "").splitlines(),
            str(inp.get("new_string") or "").splitlines(), lim.CH_CAJA),
            lim.CH_CAJA))
    elif nombre in ("Write", "Create", "New") and inp.get("content"):
        lineas = str(inp["content"]).splitlines()
        # F50b: the line-count stamp on EVERY file — a Write is all new:
        # green dot + the file's lines
        cuerpo = ("<pre>%s</pre>\n<i>%s</i>"
                  % (esc(recorte_html(
                      "\n".join("🟢 " + ln for ln in lineas[:40]),
                      lim.CH_CAJA)),
                     sello_lineas(len(lineas), 0, len(lineas))))
    elif nombre in ("Read", "View"):
        cuerpo = "<i>leyendo</i> <code>%s</code>" % esc(preview(desc, 160))
    else:
        try:
            cuerpo = "<pre>%s</pre>" % esc(recorte_html(
                json.dumps(inp, ensure_ascii=False), lim.CH_CAJA))
        except (TypeError, ValueError):
            cuerpo = ""
    return head + ("\n" + cuerpo if cuerpo else "")


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
