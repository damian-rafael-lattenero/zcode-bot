"""Selección de modelo y niveles de thinking — fuente única (F1b).

Lección del refactor del 14/10: había CUATRO MODEL_SEL con DOS shapes
incompatibles ("reasoningLevel" en gateway vs "thinking" en utils/config
— el server ignora el segundo sin error visible). Uno solo, el shape
que los golden tests congelaron: provider/modelId/options.reasoningLevel
(ver REFERENCES.md #8/#9).
"""

# Selección default del bot (dict plano: es el payload del protocolo
# session/setModel — no una entidad con comportamiento, así que dict
# consciente en vez de dataclass).
MODEL_SEL = {"providerId": "zai-bot", "modelId": "GLM-5.3",
             "options": {"reasoningLevel": "high"}}

# Niveles por modelo (REFERENCES.md #8/#9): GLM-5.3 = low/high/max sin off;
# GLM-5.2 admite un set más amplio. Default conservador para modelos nuevos.
THINKING_LEVELS = ("max", "high", "low")
MODEL_THINKING = {
    "GLM-5.3": ("max", "high", "low"),
    "GLM-5.3-Flash": ("max", "high", "low"),
}
MODELOS = ("GLM-5.3", "GLM-5.3-Flash")


def _thinking_levels(model_id):
    return MODEL_THINKING.get(model_id, THINKING_LEVELS)
