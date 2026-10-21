"""avisos — the «your choice arrived» ack component (F36).

A button's cycle has TWO feedbacks: Telegram's native 👀
(answerCallbackQuery in updates.normalize_update — "Telegram took the
tap") and this one: "the app ACCEPTED the request". A single format
for all flows (compact, stop, answer to the agent); loud=False — the
buzz belongs to the RESULT, not to the ack. Injection via parameter/
patching, as always in this house: tests patch uhttp.send or this
function.
"""
from zcode_bot.utils import http as uhttp
from zcode_bot.utils.formatting import esc


def choice_received(cfg, what, detail=""):
    """App-side ack. `what` = the choice (already readable, escaped
    here); `detail` = what comes next (optional, not escaped: the
    caller may bring formatting)."""
    text = "📬 Your choice arrived: %s" % esc(what)
    if detail:
        text += " — " + detail
    uhttp.send(cfg, text, loud=False)
