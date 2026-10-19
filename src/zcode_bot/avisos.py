"""avisos — el componente del ack «llegó tu elección» (F36).

El ciclo de un botón tiene DOS feedbacks: el 👀 nativo de Telegram
(answerCallbackQuery en updates.normalize_update — "Telegram tomó el
tap") y este: "la app ACEPTÓ el pedido". Un solo formato para todos los
flujos (compact, stop, respuesta al agente); loud=False — el buzz es
del RESULTADO, no del acuse. Inyección por parámetro/parcheo, como
siempre en la casa: los tests parchean uhttp.send o esta función.
"""
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc


def eleccion_recibida(cfg, que, detalle=""):
    """Ack del lado de la app. `que` = la elección (ya legible, se
    escapa acá); `detalle` = qué sigue (opcional, sin escapar: el
    llamador puede traer formato)."""
    texto = "📬 Llegó tu elección: %s" % esc(que)
    if detalle:
        texto += " — " + detalle
    uhttp.send(cfg, texto, loud=False)
