# F-code / R-code glossary

Comments and docstrings in this repo carry provenance tags — the
convention is `# F42: <explanation>` or a trailing `(F42)`. The
explanation next to the tag should always stand on its own; the tag is
there so the change can be grepped and dated, and decoded HERE.

- **F codes** = features / fixes, in rough chronological order
  (F0 is the first recorded bug fix; F53 the English migration).
- **R codes** = refactors from the modularization plan
  (`.opencode/plans/refactor_modularization.md`).
- **D codes** = discrepancies found in the 13/10 code audit.
- Letter suffixes (F38b, F51a…) = sub-steps of that step.
- Longer versions of the big ones: the README sections and the ADRs
  (`docs/adr/`) referenced inline below.
- `# noqa: F401,F403` in imports are flake8 codes, NOT project codes.

Anyone introducing a NEW tag adds one line to this file in the same
commit.

## F — features & fixes

| Tag | One-liner |
|---|---|
| F0 | First recorded bug: `pop(None)` left the `"nueva"` orphan in the workers store. |
| F1 | 15/10 consolidation: one single source per concept. |
| F1b | Model selection + thinking levels: single source (`models.py`). |
| F1c | The ~11× repeated proto pattern unified into "one living body" (`proto.py`). |
| F1e | `config.py` is THE single source of paths/configuration (tests rebind THAT module). |
| F2 | The `core` package: atomic state, disciplined-lock concurrency (see REFERENCES, F2–F4 backlog). |
| F2a | Move of the async transport to the `zcode_bot.telegram` package. |
| F2c | The poller's per-update body extracted out of `cmd_watch_commands`. |
| F2d | `AsyncPoller`: the async driver of the command loop (`poller.py`). |
| F3 | Webhook transport + async composition root: one update, one consumer; polling or webhook per config (ADR-011). |
| F4 | Refactor wave that produced the F4a–F4j module split (measured on the "F4+F7" session). |
| F4a | Paths resolved at call time by accessors (never frozen strings). |
| F4b | `core.py` as the shared seam between old and new modules. |
| F4c | `messages.py`: the ProtoClient message parsers. |
| F4d | `live_view.py`: sidecar of a headless turn + the anti-suicide `headless_guard`. |
| F4e | Nuclear 2.0: announced and verified full restart (`nuclear.py`, ADR-006). |
| F4f | `reporting.py`: the bot's reports and formatters. |
| F4g | `workers.py`: the bot's headless turns. |
| F4h | `updates.py`: Telegram updates plumbing. |
| F4i | `commands.dispatch`: the 2_bot dispatch. |
| F4j | `hooks.py`: the entrypoints fired by the ZCode CLI. |
| F7 | Live action: the turn streamed to the phone (CoT boxes + tool boxes) — ADR-013. |
| F7a | Spike that verified the cursor/dedupe math (0 events past the cursor). |
| F7b | Poll cadence by observed frame size (`LIVE_BIG_FRAME`), no payload accumulation. |
| F7c | Tool lifecycle via the event stream + the tool-box preset filter. |
| F8 | Warm worker + session rotation — ADR-014 (born of the 15:47 incident: flock not reentrant across fds; live streaming saturation). |
| F8a | Warm worker: t0 refresh against MAX_AGE_S; ONE full turn over an already-active session. |
| F8b | Session rotation "the dance": compact + summary turn + new session seeded. |
| F9 | Event-based turn end from the rollout transcript (`model-io-<sid>.jsonl`) — ADR-015; also its retention days. |
| F15 | ONE getUpdates reader (ADR-001): the offset is never split; sessions with their own worker skip the stuck-watch; marker cleanup is a side effect, not a notification. |
| F16 | Reload door + the `--run-turn` worker-actor entrypoint: in-flight turns are separate processes and survive poller reloads (ADR-005). |
| F17 | Explicit queue for "the Mac is busy": queue + notify + resume when freed; `/stop` clears it. |
| F19 | `trace_documents` default OFF (Damian's request). |
| F20 | turnId correlation rebuilt on the CLI logs path. |
| F21 | Orphan supervisor with TTL: answers of already-completed orphan turns still get delivered. |
| F22 | Owner writing from another chat (group) is answered THERE; model/mode survive the /task switch; short long-poll so hooks share the loop. |
| F23 | Attachments no longer fall through silently; 👀 on receipt + typing (no silent window). |
| F24 | Purges with ceilings (13/10 audit) + the unified step log with a DATE. |
| F25 | `/deploy`: the full pipeline from the phone. |
| F27 | The network-incident replay (coverage of the poller hook path). |
| F28 | A message arriving mid-rotation-dance aborts CLEANLY between phases (interruptible checks). |
| F30 | The invariant auditor — ADR-016 (five invariants; heals idempotent, recommends the rest). |
| F31 | An honest `/ctx` computed from the rollout transcript. |
| F32 | The Mac's turn arrives COMPLETE on the phone: FULL prompt/reply (no previews) + the 🖥️ Mac origin label. |
| F33 | Spectator: the Mac's turn rollout tail streamed to Telegram — ADR-017. |
| F34 | Config taxonomy + validation gate — ADR-018. |
| F34b | Shared-state FILE NAMES defined once in config. |
| F34c | The documented `loud_all` divergence (used to default to False). |
| F35b | First-cycle fail-fast: wiring bugs surface at poller startup. |
| F36 | Acks («your choice arrived») + honest ✅/kill verification. |
| F37a | Autonomous drain of dormant queues on poller cycles. |
| F37b | Slow commands run in a thread and ack immediately. |
| F38 | Slow commands IN FLIGHT (snapshot/progress), /diff as the showcase. |
| F38b | /diff file / dirty callbacks (`df:`, `dx:`). |
| F38c | The equal-parts splitter for oversized colored patches. |
| F38d | ONE renderer for git show/diff output (live and /diff share the coloring). |
| F38e | /diff history and work-in-progress shortcuts (`dl`, `dw`, `dc:`). |
| F39 | Honest `/stop`: worker phase reported + the kill verified HERE, no promised third-party reports. |
| F40 | Close-time CoT: the turn's reasoning with one button per block (`--cot-buttons`). |
| F41 | CoT LIVE: the «thinking…» box watcher that edits in place (`--cot-live`). |
| F42 | The phone's CoT as buttons that GROW per phase (replaces cot_stream text). |
| F43 | Boxes SEALED by size (the button fills up before a new one opens). |
| F43b | "Besides the button, the CoT is visible" (Damian): raw text alongside buttons. |
| F43c | The tap EDITS the message already sent (no new message per round). |
| F44 | Single-flight: a HOT watcher stays for the session's next prompt; its pid joins nuclear's kill list. |
| F45 | A turn that closes WITHOUT reasoning says so in one line. |
| F46 | «CoT R.C» labels: round R, box C; the in-progress round has its own button box. |
| F47c | The in-flight turn is marked stopped ON REQUEST («stopped by /stop», not «failed»). |
| F47d | The cache close of a turn that died without its own close (orphans_check owns it). |
| F48a | A handler returning None means «already answered» (gateway does not re-reply). |
| F48c | A round with no boxes consumes no number and sends no message. |
| F48d | Code stamp (package mtime): deploy detection — spawn a fresh watcher / reload idle workers. |
| F49 | Global 1-based index buttons over the boxes; NO global index left behind at the end. |
| F50b | The line-count stamp on EVERY file shown (Damian: «todo firmado»). |
| F51 | THE Mac↔phone model (decided 21/10): ONE shared session — the phone is an extension of the Mac, not a fork. |
| F51a | Orphan CoT caches: the honest gate + the fallback-spawn cost when the live one did not take over. |
| F51b | Queue-full (5) drop NOTICE: the oldest dropped is named, not silenced. |
| F51c | The «⏱️ N min» long-turn notice cadence. |
| F52 | Phone buttons wait 24 h (wait_timeout_s=86400; busy-flag freshness independent of it). |
| F53 | The 100% English migration (comments, strings, identifiers, persisted state with permanent bilingual readers). |
| F55 | Line numbers on every file sent to the Telegram chat (the sendDocument seam — number_lines, sibling of F50b's stamp). |
| F55b | …and on what's SHOWN of a file in the chat: /diff patches numbered from the @@ (new file for context/added, old for removed — the '⋯ line N' separators died), Write boxes numbered from 1; Edit boxes stay bare (fragment-relative numbers would lie). |

## R — refactors

| Tag | One-liner |
|---|---|
| R0b | Live-view waits with timeout=None (unbounded while the turn lives) + the long-turn notice knob. |
| R0c | Returning items to the queue PRESERVES FIFO order. |
| R2 | The gateway facade pruned: 268 lines of monolith deleted. |
| R3 | The three copies of the retry keyboard unified into one. |
| R4 | The atomic write of the flag (tmp + replace); the button-box path gates. |
| R5 | The button callback vocabulary: ONE prefix→command table (`callbacks.py`). |
| R6 | CommandSpec: a command declared ONCE (names, handler, menu) — table-driven dispatch. |
| R7 | Command argument parsing helpers (`commands/args.py`). |
| R8 | `/diff` moved verbatim to its own module (`handlers_diff.py`). |
| R9 | The closing formula of a reasoning pass shared in `reasoning.py`. |
| R10 | The live box TEXT by phase + the turn close (the 32 lines that used to close in the monolith). |
| R13 | Consolidate the CoT test fixtures: the events/thread fakes now live in support.py (FakeEventsClient/FakeThread); the session-message fakes stay local by design (different protocol surface). |

## D — discrepancies

Findings from the 13/10 code audit (kept for the two that still get
quoted):

| Tag | One-liner |
|---|---|
| D1 | The bot-command menu is visible ONLY in the owner's chat (official Telegram scope). |
| D4 | One stderr PER PID: before, every runtime appended to the same proto-stderr.log (unreadable concurrent writes). |
