"""reports — Template Method for tappable Telegram lists.

The bot's three reports (tasks, compactar, frenar) share the SAME
structure: header → numbered rows + buttons → footer, or an empty
message. The skeleton lives here; each report contributes its lambdas.
"""


def build_list_report(items, header, empty_msg, line_of, button_of,
                      footer=""):
    """Builds (text, markup) for a list with inline buttons.

    line_of(item, i) → text line (1-based i; a multi-line string keeps
    its lines — /task nests each task's subagents under it).
    button_of(item, i) → {"text", "callback_data"}, None for no button,
    or a LIST of buttons — each one gets its OWN row (F65c: the task's
    button first, then one per subagent).
    footer is appended as a separate line (with a blank line, like the monolith).
    """
    if not items:
        return empty_msg
    lines = [header]
    rows = []
    for i, item in enumerate(items, 1):
        lines.append(line_of(item, i))
        btn = button_of(item, i)
        if not btn:
            continue
        if isinstance(btn, list):
            rows.extend([b] for b in btn if b)
        else:
            rows.append([btn])
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines), {"inline_keyboard": rows}
