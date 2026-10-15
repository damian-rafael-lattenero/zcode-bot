# ADR-012: Desmembramiento del gateway por delegación

**Estado:** aceptada (2026-10-14, F4)

## Contexto

El gateway.py monolítico llegó a 4.139 líneas con 10 ciclos de dependencia
entre sus dominios internos (reports ↔ workers ↔ hooks ↔ commands ↔ …) y
una fragilidad estructural en los tests: ~8 archivos parcheaban
`zt.send`/`zt.subprocess`/`zt.time` en el namespace del gateway — parches
que quedaban MUDOS apenas la función consumidora se mudaba de módulo (la
lección del refactor "nemotron": copiar-para-modularizar terminó con dos
versiones divergentes de cada concepto).

## Decisión

1. **Mover, nunca copiar.** Un solo cuerpo vivo por concepto; el gate
   anti-duplicación (`scripts/check_no_duplicates.py`) corre en cada CI.
   Los cuerpos se mudaron TEXTUALES; el diff de cada commit A es movimiento
   puro y auditable.
2. **Seam compartido primero.** El corte raíz de los ciclos fue `core.py`
   (estado de sesión, accessors de registros, stop-markers, perm-cache,
   lecturas de cola/turnos). Regla F1e extendida a TODO: paths, registros
   y transporte se resuelven por atributo en call-time
   (`config.STATE_DIR`, `_workers()`, `uhttp.send`).
3. **Transporte por atributo.** Los módulos llaman `uhttp.send(...)`
   (con `from zcode_bot.utils import http as uhttp`). UN solo seam de
   parcheo para toda la suite: `mock.patch.object(zc_http, "send")`.
4. **Tests migran con el código, mismo commit.** Todo monkeypatch apunta
   al módulo dueño. La migración destapó bug latente: el watchdog del
   nuclear esperaba 90s REALES porque `zt.time` parcheado ya no alcanzaba
   al código mudado (suite 104s → 8.6s).
5. **Dataclasses en commit separado.** Movimiento (A) y representación
   (B) nunca en el mismo commit. Las entidades cruzan JSON en disco:
   `from_dict` tolerante a shapes viejos (la producción se lee al deploy),
   `to_dict` preserva el shape histórico, compat mapping (get/keys/
   __getitem__) para los consumidores no migrados.
6. **El orden de extracción respeta el DAG** (un ciclo de import = falta
   extraer un seam, nunca imports circulares):
   core → messages → live_view → nuclear → reporting → workers → updates
   → commands → hooks → facade.

## Resultado

- gateway.py 4.139 → 534 líneas, **3 definiciones propias**
  (process_update_sync, runtime_async, main — el composition root). El
  resto es docstring + re-exports para la suite. (La meta original <300
  era proxy de "sin lógica": la lógica es 0; los re-exports que los tests
  aún consumen vía `zt.X` son la superficie restante, migrable a futuro.)
- Módulos: core 465, messages 215, live_view 525, nuclear 383,
  reporting 813, workers 715, updates 251, hooks 422, commands/
  (registry 53 + dispatch ~800).
- LiveTurnView: los 22 nonlocals de _live_view → atributos; el bug del
  `nonlocal cot_full` (incidente 14:28) desaparece por construcción;
  batería nueva de 10 tests.
- Suite 311 tests (288 al inicio de F4), incluido el happy-path de
  prompt_worker — el corazón del bot, hasta hoy sin NINGÚN test del
  flujo completo.
- Bugs latentes encontrados al mover: `_log_rotate` sin importar
  (housekeeping), `ctx_tasks(20)` sin underscore en `_resolve_ws`
  (NameError al primer `/run <tag>`).

## División de handlers (deuda declarada)

Los ~25 `_cmd_*` viven intercalados con la tabla de registro en
`commands/dispatch.py` (movimiento puro; separarlos por dominio exigía
reordenar cuerpos). Dividir en handlers_info/turnos/sistema es refactor
mecánico posterior sobre módulo estable, con la suite de guardia.
