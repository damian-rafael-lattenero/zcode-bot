# ADR-004: stdlib-only, Python 3.9 del sistema

> **SUPERSEDED por ADR-010 (2026-10-15)**: Python 3.9 EOL, runtime 3.13
> con venv uv, aiohttp única dependencia de runtime, pytest solo-dev.
> Lo vigente de esta ADR: la disciplina de minimizar dependencias y el
> estándar de justificar cada excepción en un ADR.


**Estado:** aceptada (2026-10-12, pedido explícito del dueño)

## Decisión
Cero dependencias externas. Corre con /usr/bin/python3 (CommandLineTools
3.9) bajo LaunchAgent. Tests con unittest. La disciplina que daría un
framework viene de patrones con nombre + tests + ADRs.

## Justificación
- Daemon 24/7 en python de sistema: `pip install` ahí se rompe con cada
  update de CLT → riesgo operativo sin rollback.
- El transporte Telegram son 5 endpoints sobre urllib; los frameworks de
  bot imponen event loops que chocan con thread-per-worker + procesos hook.

## Consecuencias
- Todo patrón se implementa a mano pero documentado (Registry, Template
  Method, Repository, Strategy, Atomic Write, Command Registry).
- dataclasses sí (stdlib) cuando ayuden.
