"""R7 — helpers de parseo de argumentos de comandos.

Lo que de verdad se repetía (verificado sitio por sitio): la pareja
/qgo//qdel parseaba «sid:idx» con el MISMO try/except textual; cuatro
comandos extraían «@sess_…» y el número inicial con split(" ", 1) a
mano; y el mensaje de número fuera de lista vivía copiado con el nombre
del comando cambiado. Acá viven los tres; los comandos de dominio
seleccionan su LISTA y su accionar — el parseo común no se duplica.
"""


def parse_sid_idx(rest):
    """«sid:idx» → (sid, idx) o None si no parsea. Los callbacks de
    /queue (qgo:/qdel:) traen exactamente esto."""
    try:
        sid, idx = rest.rsplit(":", 1)
        return sid, int(idx)
    except ValueError:
        return None


def head_y_resto(rest):
    """(primer token, resto-normalizado) de los argumentos de un comando:
    el split con strip que task/stop/compact repetían inline."""
    partes = (rest or "").split(" ", 1)
    return partes[0], (partes[1].strip() if len(partes) > 1 else "")


def sid_de(head):
    """«@sess_…» → sid (el vocabulario de los botones de sesión, que
    identifican la conversación sin ambigüedad); None si no es uno."""
    return head[1:] if head.startswith("@ses") else None


def fuera_de_lista(cmd, total):
    """El aviso de número fuera de rango — mismo texto en /task, /stop y
    /compact, cambiando apenas el comando sugerido."""
    return "⚠️ Número fuera de la lista (1 a %d) — mandá /%s." % (total, cmd)
