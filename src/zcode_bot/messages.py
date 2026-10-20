"""messages — parsers de los mensajes del ProtoClient (F4c).

Funciones PURAS sobre las respuestas de session/messages: extracción de
texto/razonamiento, inventario de tool calls y cajas HTML de tools.
Nada de I/O de red ni state — el ProtoClient `c` entra por parámetro.
"""
import difflib
import re
import json
from typing import NamedTuple

from zcode_bot import limites as lim
from zcode_bot.utils.formatting import esc, preview


class ToolStep(NamedTuple):
    """Un tool call del turno para el índice de pasos (caja viva, trace).
    NamedTuple porque el shape histórico era la tupla (nombre, desc):
    los unpackings siguen intactos."""
    nombre: str
    desc: str


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
    """[ToolStep(nombre, desc)] de los tool calls de una lista de mensajes
    (parts type=='tool': state.input trae command/file_path). Tolerante al
    shape."""
    return [ToolStep(n, _tool_desc_corto(n, inp))
            for n, inp in _tools_of_messages(msgs)]


def _tools_con_call_id(msgs):
    """F7c — [(callID, nombre, input_dict, status)] de los tool calls. El
    callID (part.callID) empareja la tool del historial con los eventos
    toolCallId del stream (spike F7a: misma clave en ambos lados)."""
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
    """F22bis — [(nombre, input_dict)] COMPLETO de los tool calls (el
    input es lo que alimenta las cajas permanentes: comando entero,
    old/new de edits, content de writes). Tolerante al shape."""
    return [(n, inp) for _cid, n, inp, _st in _tools_con_call_id(msgs)]


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


def colorear_diff(lineas, hunks_sep=False):
    """Líneas de un unified diff (SIN los encabezados ---/+++) → líneas para
    el celu: 🟢 agrega / 🔴 borra / · contexto. El <pre> de Telegram no
    soporta color — el emoji codifica cada línea (pedido de Damian: verlo
    verde/rojo, uno debajo del otro). Primera línea = diffstat ● +N −M.
    UN renderer para las dos fuentes (F38d): la caja viva (difflib sobre
    old/new_string del Edit) y /diff (salida de git). Los @@ se tiran en
    las cajas (ruido en chats chicos) o se vuelven '⋯ línea N' con
    hunks_sep — en un archivo entero los saltos entre hunks sí importan."""
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
    """Caja viva del tool Edit: difflib sobre old/new_string → colorear_diff."""
    d = list(difflib.unified_diff(viejo, nuevo, lineterm=""))[2:]
    return "\n".join(colorear_diff(d))[:max_chars] or "(sin cambios)"


# presets del filtro de cajas (config tools_caja_filtro — F7c+)
_IDX_SOLO = {"Read", "View", "Grep", "Glob", "LS", "TodoWrite", "TodoRead",
             "AskUserQuestion", "Skill", "ListModels", "ListMCPResources",
             "Task", "TaskOutput", "TaskStop"}
_MINIMO = {"Bash", "Edit", "MultiEdit", "Replace", "Write", "Create", "New",
           "NotebookEdit"}


def _tool_caja_on(nombre, cfg):
    """¿Esta tool tiene CAJA permanente propia? Presets por config
    tools_caja_filtro: 'todas' | 'accion' (default — todo lo que HACE
    algo: bash, ediciones, subagentes, MCP, web) | 'minimo' (bash+ediciones).
    Las que no tienen caja siguen visibles: índice de pasos + caja viva."""
    modo = (cfg or {}).get("tools_caja_filtro") or "accion"  # cfg puede ser {}
    if modo == "todas":
        return True
    if modo == "minimo":
        return nombre in _MINIMO
    return nombre not in _IDX_SOLO


def _tool_caja(nombre, inp):
    """F22bis — texto de la caja PERMANENTE de un tool call: comando
    COMPLETO (pedido de Damian), diff real para edits/writes. Cap 3400
    chars (Telegram 4096 - formato). Devuelve HTML."""
    desc = _tool_desc_corto(nombre, inp)
    head = "🔧 <b>%s</b>%s" % (esc(nombre),
                               (" — <code>%s</code>" % esc(preview(desc, 120)))
                               if desc else "")
    cuerpo = ""
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    if isinstance(cmd, str) and cmd.strip():
        cuerpo = "<pre>%s</pre>" % esc(cmd[:lim.CH_CAJA])
        sal = inp.get("output") or inp.get("result") or ""
        if isinstance(sal, str) and sal.strip():
            cuerpo += "\n└ <pre>%s</pre>" % esc(sal[:800])
    elif isinstance(inp.get("edits"), list) and inp["edits"]:
        # MultiEdit: UN diff coloreado por edit (antes caía al fallback
        # de JSON crudo — «lo vi todo junto y no se entendió»)
        trozos = []
        for i, ed in enumerate(inp["edits"], 1):
            if not isinstance(ed, dict):
                continue
            trozos.append("· edit %d\n%s" % (
                i, _diff_coloreado(str(ed.get("old_string") or "")
                                   .splitlines(),
                                   str(ed.get("new_string") or "")
                                   .splitlines(), lim.CH_CAJA // max(1, len(inp["edits"])))))
        cuerpo = "<pre>%s</pre>" % esc("\n".join(trozos)[:lim.CH_CAJA])
    elif nombre in ("Edit", "MultiEdit", "Replace") and inp.get("old_string") is not None:
        cuerpo = "<pre>%s</pre>" % esc(_diff_coloreado(
            str(inp.get("old_string") or "").splitlines(),
            str(inp.get("new_string") or "").splitlines(), lim.CH_CAJA))
    elif nombre in ("Write", "Create", "New") and inp.get("content"):
        lineas = str(inp["content"]).splitlines()
        cuerpo = ("<pre>%s</pre>\n<i>(+%d líneas)</i>"
                  % (esc("\n".join("🟢 " + ln
                                   for ln in lineas[:40])[:lim.CH_CAJA]),
                     len(lineas)))
    elif nombre in ("Read", "View"):
        cuerpo = "<i>leyendo</i> <code>%s</code>" % esc(preview(desc, 160))
    else:
        try:
            cuerpo = "<pre>%s</pre>" % esc(json.dumps(inp, ensure_ascii=False)[:lim.CH_CAJA])
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
