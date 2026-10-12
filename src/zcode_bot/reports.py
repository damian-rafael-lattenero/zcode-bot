"""reports — Template Method para listas tocables de Telegram.

Los tres reportes del bot (tasks, compactar, frenar) comparten la MISMA
estructura: header → filas numeradas + botones → footer, o mensaje de
vacío. Acá vive el esqueleto; cada reporte aporta sus lambdas.
"""


def build_list_report(items, header, empty_msg, line_of, button_of,
                      footer=""):
    """Construye (texto, markup) para una lista con botones inline.

    line_of(item, i) → línea de texto (1-based i).
    button_of(item, i) → {"text", "callback_data"} o None para sin botón.
    footer se agrega como línea separada (con blank line, como el monolito).
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
