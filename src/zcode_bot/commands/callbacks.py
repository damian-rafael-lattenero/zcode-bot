"""R5 — el vocabulario de callbacks de botones: UNA tabla.

Cada botón inline que el bot manda lleva un callback_data corto (≤64 B,
límite de Telegram). Su traducción a "lo que el dueño tipeó" vivía en un
if-chain de 26 ramas (updates._cb_text) mientras los ~20 PRODUCTORES del
prefijo estaban scattereados por reporting/handlers/workers/hooks/
razonamiento — conocimiento partido, contrato implícito. Acá vive la
tabla única: los estilos cubren las cuatro formas reales (argumento
crudo, argumento con «:»→espacio, sesión con @, texto libre) y los
exactos sin argumento.

Notas de compatibilidad que la tabla PRESERVA a propósito:
- t:ses / s:ses / c:ses matchean solo con «ses…»: un «t:x» raro cae al
  default (texto libre), como siempre.
- task:N es el vocabulario VIEJO (posición en la lista completa, ambiguo
  entre workspaces) — botones viejos aún viven en los chats del dueño.
- «planfix» (hooks: «✏️ Agregar algo al plan») NO está mapeado: cae al
  default y viaja como texto libre — que en contexto de permiso pendiente
  llega como respuesta al agente. Es el comportamiento de hoy, hecho
  explícito acá para que deje de ser un accidente silencioso.
"""
import re

# estilos de argumento
_AR = "ar"        # "/cmd " + resto (los «:» quedan: sid:idx)
_ESP = "esp"      # "/cmd " + resto con «:» → « »
_SES = "ses"      # "/cmd @" + resto (identifica la conversación)
_LIBRE = "libre"  # el resto tal cual: NO es comando, es texto del dueño

# tokens EXACTOS (sin argumento)
EXACTOS = {
    "retry": "/retry",
    "yes": "si",
    "no": "no",
    "nuc:go": "/nuclear go",   # confirmación ☢️ (dos pasos)
    "nuc:no": "/nuclear no",
    "dep:go": "/deploy go",    # confirmación 🚀 (dos pasos)
    "dep:no": "/deploy no",
    "dl": "/diff log",         # F38e: historial
    "dw": "/diff",             # F38e: trabajo en curso
}

# prefijos con argumento: prefijo → (comando, estilo)
PREFIJOS = {
    "t:ses": ("/task", _SES),       # t:sess_... (y ses_ viejo)
    "s:ses": ("/stop", _SES),
    "c:ses": ("/compact", _SES),
    "mo:": ("/mode", _AR),          # Plan mode / Full access
    "qgo:": ("/qgo", _AR),          # F22: lanzar YA un encolado (sid:idx)
    "qdel:": ("/qdel", _AR),        # F22: descartar un encolado
    "m:": ("/model", _AR),
    "th:": ("/thinking", _AR),
    "task:": ("/all_tasks", _AR),   # vocabulario viejo (posición)
    "ans:": ("", _LIBRE),           # respuesta textual a una pregunta
    "df:": ("/diff", _ESP),         # F38b: archivo n [+hash +parte]
    "dx:": ("/diff dirty", _ESP),   # F38b: cambios sin commit
    "dc:": ("/diff commit", _AR),   # F38e: un commit de la lista
    "ct:": ("/cotb", _ESP),         # F40: caja n [parte p] del CoT cacheado
}

# los prefijos más largos primero: si mañana hay «t:» y «t:ses», gana el
# específico (hoy no hay colisión, el orden lo hace a prueba de futuro)
_PREFIJOS_ORD = sorted(PREFIJOS.items(), key=lambda kv: -len(kv[0]))


def texto_de_callback(data):
    """callback_data → el texto que el dueño «tipeó». Default: los
    primeros 64 chars como texto libre (comportamiento histórico)."""
    if data in EXACTOS:
        return EXACTOS[data]
    for pref, (cmd, estilo) in _PREFIJOS_ORD:
        if data.startswith(pref):
            # _SES matchea con «ses…» pero consume SOLO las 2 letras del
            # prefijo real (t:/s:/c:) — la sesión va ENTERA tras la @,
            # igual que el if-chain histórico
            resto = data[2:] if estilo == _SES else data[len(pref):]
            if estilo == _ESP:
                return "%s %s" % (cmd, resto.replace(":", " "))
            if estilo == _SES:
                return "%s @%s" % (cmd, resto)
            if estilo == _LIBRE:
                return resto
            return "%s %s" % (cmd, resto)
    return data[:64]


def es_respuesta_de_pregunta(data):
    """R5 — ¿el callback responde una pregunta/permiso pendiente del
    agente? El guard de pregunta vencida (antes inline en gateway)
    lee esto: sin pregunta pendiente el botón NO se inyecta a la
    conversación activa."""
    return bool(data) and (data.startswith("ans:") or data in ("yes", "no"))


def callback_data_valido(data):
    """Los ≤64 BYTES de Telegram (sid real de 41 chars + t0 + n + p = 61
    en el peor caso real — el test de F40 lo asgura por construcción)."""
    return bool(data) and len(data.encode()) <= 64 and not re.search(
        r"[\s\n]", data)
