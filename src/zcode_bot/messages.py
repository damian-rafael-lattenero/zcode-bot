"""messages — parsers de los mensajes del ProtoClient (F4c).

Funciones PURAS sobre las respuestas de session/messages: extracción de
texto/razonamiento, inventario de tool calls y cajas HTML de tools.
Nada de I/O de red ni state — el ProtoClient `c` entra por parámetro.
"""
import json

from zcode_bot.utils.formatting import esc, preview


def _extract_assistant_text(msgs, solo_ultima=False):
    """Texto de los mensajes assistant: SOLO parts con type=="text" (sin
    reasoning/tool noise). Estructura real: {info:{role,id}, parts:[{type,text}]}."""
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
    """[(nombre, desc)] de los tool calls de una lista de mensajes (parts
    type=='tool': state.input trae command/file_path). Tolerante al shape."""
    return [(n, _tool_desc_corto(n, inp))
            for n, inp in _tools_of_messages(msgs)]


def _tools_of_messages(msgs):
    """F22bis — [(nombre, input_dict)] COMPLETO de los tool calls (el
    input es lo que alimenta las cajas permanentes: comando entero,
    old/new de edits, content de writes). Tolerante al shape."""
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
            out.append((nombre, inp))
    return out


def _tool_desc_corto(nombre, inp):
    """Desc de una línea (índice de pasos, caja vija, trace)."""
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


def _tool_caja(nombre, inp):
    """F22bis — texto de la caja PERMANENTE de un tool call: comando
    COMPLETO (pedido de Damian), diff real para edits/writes. Cap 3400
    chars (Telegram 4096 - formato). Devuelve HTML."""
    import difflib
    LOW = 3400
    desc = _tool_desc_corto(nombre, inp)
    head = "🔧 <b>%s</b>%s" % (esc(nombre),
                               (" — <code>%s</code>" % esc(preview(desc, 120)))
                               if desc else "")
    cuerpo = ""
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    if isinstance(cmd, str) and cmd.strip():
        cuerpo = "<pre>%s</pre>" % esc(cmd[:LOW])
        sal = inp.get("output") or inp.get("result") or ""
        if isinstance(sal, str) and sal.strip():
            cuerpo += "\n└ <pre>%s</pre>" % esc(sal[:800])
    elif nombre in ("Edit", "MultiEdit", "Replace") and inp.get("old_string") is not None:
        viejo = str(inp.get("old_string") or "").splitlines()
        nuevo = str(inp.get("new_string") or "").splitlines()
        d = list(difflib.unified_diff(viejo, nuevo, lineterm=""))[2:] or ["(sin cambios)"]
        cuerpo = "<pre>%s</pre>" % esc("\n".join(d)[:LOW])
    elif nombre in ("Write", "Create", "New") and inp.get("content"):
        lineas = str(inp["content"]).splitlines()
        cuerpo = ("<pre>%s</pre>\n<i>(+%d líneas)</i>"
                  % (esc("\n".join(lineas[:40])[:LOW]), len(lineas)))
    elif nombre in ("Read", "View"):
        cuerpo = "<i>leyendo</i> <code>%s</code>" % esc(preview(desc, 160))
    else:
        try:
            cuerpo = "<pre>%s</pre>" % esc(json.dumps(inp, ensure_ascii=False)[:LOW])
        except (TypeError, ValueError):
            cuerpo = ""
    return head + ("\n" + cuerpo if cuerpo else "")


def _reasoning_of_messages(msgs):
    """Razonamiento completo y EN ORDEN de los mensajes assistant
    (parts type=='reasoning' — el CoT queda persistido en el historial)."""
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
