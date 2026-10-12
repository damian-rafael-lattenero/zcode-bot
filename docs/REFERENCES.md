# REFERENCES — hechos verificados contra documentación oficial

Cada decisión del refactor se respalda acá. Fuente + fecha de verificación.
Verificado: 2026-10-12 (búsquedas via serper + lecturas de páginas oficiales).

## Telegram Bot API (core.telegram.org)

1. **callback_data: 1-64 bytes** — InlineKeyboardButton, core.telegram.org/bots/api#inlinekeyboardbutton.
   ✔ El código cumple; tests/test_golden.py lo aserta (`<= 64`).
2. **getUpdates NO funciona con webhook seteado** — core.telegram.org/bots/api#getupdates.
   Además, dos consumidores simultáneos de getUpdates chocan (409 Conflict) —
   validado empíricamente en este proyecto (conflicto con el bot integrado).
   → ADR-001: un solo lector (poller), coordinación vía flag tg-busy.
3. **offset**: un update queda confirmado cuando getUpdates recibe un offset
   mayor que su update_id; los confirmados se olvidan. → save_offset(+1).
4. **answerCallbackQuery**: requiere llamarse para cerrar el spinner del
   botón tocado. → fetch_updates lo hace best-effort.
5. **Rate limits** — core.telegram.org/bots/faq: ~1 mensaje/segundo por chat
   (ráfagas cortas toleradas), ~30/seg global broadcast, 20/min por grupo.
   → _live_edit cada ≥3 s y sleep 5 s ante 429 cumplen holgado.
6. **parse_mode HTML** exige escapar (`<`, `>`, `&`). → esc() en todo texto
   dinámico; congelado por golden tests.
7. **setMyCommands acepta `scope` (BotCommandScope) y hasta 100 comandos** —
   core.telegram.org/bots/api#setmycommands.
   ⚠️ DISCREPANCIA: registramos el menú SIN scope (global). Mejora F3/F4:
   BotCommandScopeChat(chat_id) para que el menú sea solo del dueño.

## z.ai / GLM

8. **GLM-5.3: tres niveles de thinking — low, high, max — y DESACTIVAR thinking
   ya no está soportado** — z.ai/blog/glm-5.3 (post oficial).
   ✔ Confirma el fix urgente del 2026-10-12 (THINKING_LEVELS = max/high/low;
   "medium" era inválido y rompía). Test lo congela.
9. **GLM-5.2 admite un set más amplio** (max/xhigh/high/medium/low/minimal/none)
   — docs.z.ai/guides/capabilities/thinking.
   ⚠️ NOTA F4: si el plan suma modelos, THINKING_LEVELS debe derivarse del
   modelo activo, no ser constante global.

## ZCode (local, verificado empíricamente — no hay doc pública del protocolo)

10. **Hooks: exactamente 7 eventos soportados**, config en
    ~/.zcode/cli/config.json con `hooks.enabled: true` — guía oficial de
    configuración (skill zcode-guide). ✔ Nuestra config usa 6.
11. **modelSelection shape**: `{providerId, modelId, options:{reasoningLevel}}`
    — verificado contra runtime/model_selection de session_entry (DB de la app).
12. **Un runtime NO ve los turnos de otro runtime** (app vs headless) y NO
    existe stop cruzado en el protocolo: 9 métodos candidatos → -32601
    (probado en vivo 2026-10-12). → ADR-002: /stop frena con el MISMO cliente
    del worker; ADR-003: turnos de la app solo se frenan en la app.
13. **Números de compactación autoritativos**: `preCompactTokenCount` y
    `truePostCompactTokenCount` (el post sin recorte puede ser MAYOR que pre)
    — parts `compaction` en session/messages, probado en vivo.

## Discrepancias detectadas (backlog F2–F4)

| # | Hallazgo | Destino |
|---|----------|---------|
| D1 | Menú de comandos registrado sin scope (global) | F3: BotCommandScopeChat |
| D2 | busy_fresh (120 s) < wait_timeout_s (240 s) → ventana de carrera del offset | F4 |
| D3 | THINKING_LEVELS constante global vs set por modelo (GLM-5.2≠5.3) | F4 |
| D4 | proto-stderr.log: appends concurrentes de varios runtimes | F4: por PID |
