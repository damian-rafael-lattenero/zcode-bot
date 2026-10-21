"""reports — Template Method for tappable Telegram lists.

The bot's three reports (tasks, compactar, frenar) share the SAME
structure: header → numbered rows + buttons → footer, or an empty
message. The skeleton lives here; each report contributes its lambdas.
"""


def build_list_report(items, header, empty_msg, line_of, button_of,
                      footer=""):
    """Builds (text, markup) for a list with inline buttons.

    line_of(item, i) → text line (1-based i).
    button_of(item, i) → {"text", "callback_data"} or None for no button.
    footer is appended as a separate line (with a blank line, like the monolith).
    """
    if not items:
        return empty_msg
    lines = [header]
    filas = []
    for i, item in enumerate(items, 1):
        lines.append(line_of(item, i))
        btn = button_of(item, i)
        if btn:
            filas.append([btn])
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines), {"inline_keyboard": filas}
