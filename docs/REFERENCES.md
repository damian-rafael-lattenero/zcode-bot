# REFERENCES — facts verified against official documentation

Every decision of the refactor is backed here. Source + date of
verification. Verified: 2026-10-12 (serper searches + readings of the
official pages).

## Telegram Bot API (core.telegram.org)

1. **callback_data: 1-64 bytes** — InlineKeyboardButton,
   core.telegram.org/bots/api#inlinekeyboardbutton.
   ✔ The code complies; tests/test_golden.py asserts it (`<= 64`).
2. **getUpdates does NOT work while a webhook is set** —
   core.telegram.org/bots/api#getupdates. Also, two simultaneous
   consumers of getUpdates collide (409 Conflict) — validated
   empirically in this project (conflict with the built-in bot).
   → ADR-001: a single reader (the poller), coordination via the
   tg-busy flag.
3. **offset**: an update is confirmed once getUpdates receives an offset
   greater than its update_id; confirmed ones are forgotten.
   → save_offset(+1).
4. **answerCallbackQuery**: must be called to close the spinner of the
   tapped button. → fetch_updates does it best-effort.
5. **Rate limits** — core.telegram.org/bots/faq: ~1 message/second per
   chat (short bursts tolerated), ~30/s global broadcast, 20/min per
   group. → _live_edit at ≥3 s intervals and the 5 s sleep on 429 comply
   with ample margin.
6. **parse_mode HTML** requires escaping (`<`, `>`, `&`). → esc() on all
   dynamic text; frozen by the golden tests.
7. **setMyCommands accepts `scope` (BotCommandScope) and up to 100
   commands** — core.telegram.org/bots/api#setmycommands.
   ⚠️ DISCREPANCY: we register the menu WITHOUT scope (global). F3/F4
   improvement: BotCommandScopeChat(chat_id) so the menu is the owner's
   only.

## z.ai / GLM

8. **GLM-5.3: three thinking levels — low, high, max — and DISABLING
   thinking is no longer supported** — z.ai/blog/glm-5.3 (official post).
   ✔ Confirms the urgent fix of 2026-10-12 (THINKING_LEVELS =
   max/high/low; "medium" was invalid and broke things). A test freezes
   it.
9. **GLM-5.2 accepts a wider set**
   (max/xhigh/high/medium/low/minimal/none) —
   docs.z.ai/guides/capabilities/thinking.
   ⚠️ F4 NOTE: if the plan adds models, THINKING_LEVELS must be derived
   from the active model, not be a global constant.

## ZCode (local, verified empirically — no public protocol docs)

10. **Hooks: exactly 7 events supported**, configured in
    ~/.zcode/cli/config.json with `hooks.enabled: true` — official
    configuration guide (zcode-guide skill). ✔ Our config uses 6.
11. **modelSelection shape**:
    `{providerId, modelId, options:{reasoningLevel}}`
    — verified against runtime/model_selection of session_entry (the
    app's DB).
12. **One runtime does NOT see another runtime's turns** (app vs
    headless) and there is NO cross-stop in the protocol: 9 candidate
    methods → -32601 (tested live on 2026-10-12). → ADR-002: /stop halts
    with the worker's OWN client; ADR-003: app turns can only be halted
    in the app.
13. **Authoritative compaction numbers**: `preCompactTokenCount` and
    `truePostCompactTokenCount` (the un-trimmed post can be LARGER than
    pre) — `compaction` parts in session/messages, tested live.
14. **There is no external entry into the app's runtime**: the host's
    `zcode-cli` children expose UDS `znr-*.sock` that do not answer the
    protocol (live probe, 0 bytes), and zai-org/feedback#890 (Desktop
    3.14.4) documents that the desktop runtime "communicates only over
    private stdio with the app; no secondary local listener for chat" —
    github.com/zai-org/feedback/issues/890.
    → ADR-006: /nuclear closes and reopens the app.
15. **Quitting ZCode entirely is safe for config and sessions**: "With
    ZCode fully quit and no background commands running, configuration
    and session records are unaffected" — official FAQ,
    zcode.z.ai/en/docs/qa.
    → Safety basis of the /nuclear restart cycle (ADR-006).
16. **sendDocument accepts files via multipart/form-data** (50 MB limit;
    the chat viewer scrolls the whole file) —
    core.telegram.org/bots/api#senddocument.
    → ADR-007: a turn's full CoT (115K+ chars) travels as a .txt
    document; the 4096-char messages only carry the live tail.
17. **`str += x` with a single reference is amortized O(1) in CPython**
    (interpreter optimization; in PyPy it IS quadratic) —
    bugs.python.org/issue980695 ·
    pypy.org/posts/2023/01/string-concatenation-quadratic.html.
    → ADR-008: our delta buffers do NOT need join().
18. **Circuit Breaker and Bulkhead are canonical resilience patterns**
    (degrade instead of dying / isolate in pools) —
    learn.microsoft.com/en-us/azure/architecture/patterns/ (the Azure
    Architecture Center's official catalog; originated with Release It!,
    Nygard 2007).
    → ADR-008: EVENTS_TRIP degrades the live view; ctx_probe isolated
    memory (retired in F31: the per-process workers are the living
    bulkhead).

## Discrepancies found (F2–F4 backlog)

| # | Finding | Destination |
|---|---------|-------------|
| D1 | Command menu registered without scope (global) | F3: BotCommandScopeChat |
| D2 | busy_fresh (120 s) < wait_timeout_s (240 s) → offset race window | F4 |
| D3 | THINKING_LEVELS global constant vs per-model set (GLM-5.2≠5.3) | F4 |
| D4 | proto-stderr.log: concurrent appends from several runtimes | F4: by PID |
