# ADR-005: Deploy with a reload-now door, backup and rollback

**Status:** accepted (2026-10-12)

## Decision
`deploy.sh`: tests → timestamped backup → copy → py_compile → ritual
(running_turns == 1) → package import check → `state/reload-now` door →
new-PID verification. Rollback: `--rollback` restores the last backup +
kickstart.

## Rationale
- The poller was NEVER restarted with bot runs in flight (the door waited
  for HEADLESS_WORKERS to be empty): no deliveries were lost. Since F16
  the door is instant: in-flight turns are separate processes that do not
  depend on the poller.
- The ritual was born from real incidents (half-finished edits that
  launchd executed → IndentationError in a loop). It grew with ADR-012:
  anti-dup gate + `running_turns` in core.py + gateway as a pure facade
  of exactly 3 defs; the import gate runs BEFORE copying (lesson from
  10/15: a post-copy gate once left a non-importing version installed).

## Consequences
- Safe deploy even with the bot in use; the gate is autonomous.
- LaunchAgent and hook paths INTACT (17-line shim).
