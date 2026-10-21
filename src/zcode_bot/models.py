"""Model selection and thinking levels — single source (F1b).

Lesson from the 14/10 refactor: there were FOUR MODEL_SELs with TWO
incompatible shapes ("reasoningLevel" in gateway vs "thinking" in
utils/config — the server ignores the latter with no visible error).
One only, the shape the golden tests froze:
provider/modelId/options.reasoningLevel (see REFERENCES.md #8/#9).
"""

# The bot's default selection (plain dict: it is the session/setModel
# protocol payload — not an entity with behavior, hence a deliberate
# dict instead of a dataclass).
MODEL_SEL = {"providerId": "zai-bot", "modelId": "GLM-5.3",
             "options": {"reasoningLevel": "high"}}

# Levels per model (REFERENCES.md #8/#9): GLM-5.3 = low/high/max with no off;
# GLM-5.2 accepts a wider set. Conservative default for new models.
THINKING_LEVELS = ("max", "high", "low")
MODEL_THINKING = {
    "GLM-5.3": ("max", "high", "low"),
    "GLM-5.3-Flash": ("max", "high", "low"),
}
MODELOS = ("GLM-5.3", "GLM-5.3-Flash")


def _thinking_levels(model_id):
    return MODEL_THINKING.get(model_id, THINKING_LEVELS)
