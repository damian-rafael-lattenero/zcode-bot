"""limites — constantes COMPARTIDAS entre módulos (F34, ADR-018).

Tres hogares, uno por clase de valor: los KNOBS (tunables por el usuario
desde bot-config.json) viven SOLO en config.DEFAULTS; las constantes de
DOMINIO (privadas de un módulo: COT_CHUNK, CAP_S...) viven en su módulo
dueño; lo COMPARTIDO entre módulos vive ACÁ, con el porqué de cada uno.
El gate (scripts/check_no_duplicates.py) exige una sola definición por
nombre — definir acá algo que ya existe en otro módulo rompe la suite.
"""

# Telegram acepta 4096 chars por mensaje: TODO troceo del chat deriva
# de este techo, cada constante reserva su margen de formato
TELEGRAM_MSG_MAX = 4096
CH_TURNO = 3800        # F32: trozo del turno completo de la Mac (header + sufijo de paginado)
PARTES_MAX = 6         # ~22.8k chars de output; más → la última marca el recorte
CH_RESPUESTA = 3900    # delivery del worker (_send_long_reply) y cap de send()
CH_CAJA = 3400         # caja de tool (_tool_caja): comando/diff completo
