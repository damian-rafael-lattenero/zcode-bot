# ADR-006 — /nuclear: emergency reset by closing and reopening the app

**Status:** accepted (2026-10-12)
**Context:** /stop only halts runs started from the phone (ADR-002: one
runtime cannot see another's turns). Tasks running in the Mac UI have no
external brake — and in an emergency (walking away while tokens keep
burning) everything must be cut off from the phone.

## Investigation (why there is NO clean path)

- **Live probe** (2026-10-12): the `zcode-cli` children of
  `zcode-host-local` each expose a UDS `znr-*.sock`; speaking the
  `.`-framed protocol to it with `session/list` returns nothing (0 bytes).
  The host only has anonymous pipes with the app — no external listener.
- **zai-org/feedback#890** (research on Desktop 3.14.4): "The
  desktop's agent runtime communicates only over private stdio with the app;
  I found no secondary local listener for chat" — it requests exactly the
  feature we would need (local ingress into an open app session). It does
  not exist today.
- **Official FAQ** (zcode.z.ai/en/docs/qa): "With ZCode fully quit and no
  background commands running, configuration and session records are
  unaffected" — closing the app entirely is SAFE for config and sessions.

## Decision

`/nuclear` (alias `/nuke`), TWO-step confirmation (only the button
produces `go`; typing "nuclear" only shows the confirmation):

1. `session/stop` to each of the bot's workers (ADR-002: same runtime,
   real stop).
2. SIGTERM to the app-server runtimes children of the poller (a guarantee
   even if some stop fails; the workers see it as "app-server died").
3. `killall -TERM ZCode` (normal close, WITHOUT osascript permissions) →
   if it does not close within 20 s, rescue SIGKILL. Everything in the UI
   dies here.
4. The orphaned host is terminated ONLY if the app is already dead (the
   sole exception to the "never kill zcode-host-local" rule: the rule
   protects the live-app case).
5. The orphaned `zcode-cli` processes (ppid 1, reparented to launchd when
   the host died) get SIGTERM (rescue SIGKILL). With no survivors left in
   the tree there are no registrations for the Dock to show as "exec"
   icons (it happened on the first real /nuclear: 3 cli icons from the
   old tree).
6. Only once the WHOLE tree is dead: `open -a ZCode` + verification (40 s).
7. `launchctl kickstart -k` of the bot. Safe: `save_offset` precedes the
   command dispatch → the /nuclear is not reprocessed on startup.

## Consequences

- The bot does NOT depend on the app (LaunchAgent poller + its own
  runtimes): it survives the cycle and it is the one reporting the result.
- Unsaved state in app editors is lost (accepted: it is an emergency; the
  official docs say config/sessions are unaffected).
- If local ingress appears one day (feedback#890), steps 3-5 are replaced
  by `session/stop` against the app's runtime: the command is already
  isolated in `app_restart_cycle`.
