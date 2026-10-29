# ADR-022: live mode — write into the real conversation via a fork channel

Date: 2026-10-29
Status: accepted

## Context

z.ai's own Telegram bot writes into the REAL conversation of the app,
interrupts the visible turn, and mirrors exactly what the screen shows.
Research in the fork (2026-10-29) found why, and it is not the cloud:

- The bot is a feature INSIDE the desktop app (`packages/services/src/bots/`).
  The app long-polls api.telegram.org itself (`telegramChannelRuntime.ts`)
  with a user-created BotFather token.
- Inbound messages go through the SAME service path as the UI composer:
  `handleInboundMessage` -> `IZCodeTaskService.sendPrompt` -> V4 `sendText`
  -> the CLI agent's CommandInbox FIFO. The code says it outright: "the bot
  is just another input of the same session".
- `/stop` from the chat triggers the same V4 stop as the UI's stop button.
- The mirroring comes from subscribing to the same task event stream with
  delivery kind `bot-channel-continuous`.
- An EXTERNAL process cannot replicate this against the running app: no
  local socket or port exists, the agent's stdio is private to the Host
  process, and resuming the session on disk from outside spawns a second
  runtime with a new logEpoch (snapshots invalidated, non-admitted inputs
  lost). This is why our bot has always been a parallel headless runtime
  plus read-only observation of the app.

## Decision

1. **Live is the default mode.** The fork gains a "daemon local" channel: a
   unix socket exposing inbound-message handling (`handleInboundMessage`)
   and a subscription to the bot-channel-continuous stream. The bots
   service already abstracts channels (telegram/feishu/weixin/webhook), so
   the daemon is just another channel — architecturally native.
2. **The Kotlin daemon owns Telegram and the commands** (its own token,
   CoT boxes, /task /status /deploy /brain...); FREE TEXT forwards through
   the socket into the live conversation, and /stop maps to the V4 stop.
3. **/mode headless** keeps the current architecture (owned runtime) for
   background work: the 5-message queue, background subagents, turns that
   must not touch the conversation on screen, and operation while the app
   is closed.

What headless still buys (why it stays as a toggle, not a grave): isolated
turns, the queue, app-independent operation, crash isolation.

## Consequences

- B3 re-scoped into B3a (fork channel) + B3b (daemon routing) — see
  docs/kap-first-migration.md section 6.0.
- Cascading simplifications in live mode: no owned stdio client, no
  turn-end race (the stream IS the app's), no kill-loop class for /stop,
  the spectator becomes redundant, F20-style turn-correlation bugs are
  impossible by design.
- The official Bots feature and our daemon can coexist (different tokens),
  but only one thing should drive a given conversation; the daemon's
  routing flag is the arbiter.
- Two-runtime-on-one-session remains forbidden: the daemon never touches
  the session files directly in live mode.
