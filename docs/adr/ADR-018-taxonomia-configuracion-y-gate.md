# ADR-018 — Taxonomía de configuración: tres hogares + gate

**Estado**: aceptado (2026-10-19). Contexto: «mil magic numbers por todos
lados» (Damian) — inventario F34: ~565 literales en los 15 peores
archivos de src, el tamaño de mensaje de Telegram vivo como 6 números
distintos, 3 estilos de fallback de knobs, 7 knobs que solo existían
como fallbacks inline, paths de estado escritos a mano en ~20 sitios y
24 referencias a nombres de archivo sueltos.

## Decisión — tres hogares, uno por clase de valor

1. **KNOB** (tunable por el usuario desde bot-config.json) → vive SOLO
   en `config.DEFAULTS`, con comentario. El código lo lee con
   `cfg.get("clave")` SIN fallback (load_config ya mergea DEFAULTS).
   Un guard `or` residual es protección contra `null` explícito del
   JSON, no duplicación. F34c corrigió 2 divergencias reales:
   `loud_all` (utils/http hacía `.get("loud_all", False)` — invertía el
   sonido fuera de load_config) y `suppress_error_patterns` (hooks
   perdía las 5 regex de DEFAULTS con su fallback `[]`).
2. **CONSTANTE DE DOMINIO** (privada de un módulo: COT_CHUNK, CAP_S,
   LOG_TAIL...) → UPPER_CASE comentada en su módulo dueño. No se
   centraliza: el mega-archivo cajón-de-sastre pudre.
3. **CONSTANTE COMPARTIDA** (entre módulos) → `limites.py`, chico y
   con el porqué: TELEGRAM_MSG_MAX=4096 + derivados (CH_TURNO 3800,
   CH_RESPUESTA 3900, CH_CAJA 3400, PARTES_MAX 6).
4. **PATHS** → nombres de archivo en config (`ARCH_COLA`,
   `ARCH_SESION`...) + constructores (`marker_path`, `stop_dir`,
   `rollout_path`) — un rename hoy es tocar UNA lista.

## Enforcement — scripts/check_no_duplicates.py (F34e)

El gate de F5 creció tres verificaciones AST:
1. **Duplicados** (original): un símbolo, un cuerpo.
2. **Atributos de módulo**: todo `config.X` / `lim.X` UPPER referenciado
   debe existir en el dueño — existe por el incidente del 19/10:
   `ARCH_RELOAD` faltante dejó el poller sordo 5 minutos EN DEADLOCK
   (el mecanismo de reload leía el mismo atributo faltante; ni el
   suite ni el import gate lo agarraban porque es atributo, no import).
3. **Magic numbers**: keyword `timeout=`/similares, slices ≥1000 y
   `cfg.get` con fallback literal son violación salvo ALLOW_MAGIC
   (lo preexistente auditado; lo nuevo se nombra o se justifica).

## Consecuencias

- Cambiar un límite/tiempo es tocar un nombre y la suite describe el
  mismo contrato (los tests importan las constantes reales via support).
- La allowlist del gate es explícita y acotada: crecerla exige una
  línea con justificación, no es gratis volver a los mil números.
- Fuera de scope: dataclass/pydantic para config (sigue el dict),
  literales triviales (índices, recortes de log sin semántica), y un
  fake clock compartido para tests de timing (parked).
