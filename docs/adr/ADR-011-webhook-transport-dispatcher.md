# ADR-011 — Update transport: single serial dispatcher, polling or webhook (mutually exclusive)

**Status:** accepted (2026-10-15). **Partially supersedes ADR-001** (a
single getUpdates reader): the invariant is no longer "the poller is the
only one calling getUpdates" but "THE DISPATCHER is the only one
processing updates" — the transport is interchangeable.

**Context:** the 10/14 network incident (Connection reset / SSL timeouts
to api.telegram.org, mitigated with an /etc/hosts pin) motivated the
webhook: Telegram PUSHES the updates and the long-poll disappears (and
with it, that class of failures). The attempt in commit `952b50f` was
left broken (no auth, no owner check, nonexistent handlers) and was
burned down in F0.

## Decision

1. **One pipeline, two transports**: `normalize_update` →
   `UpdateDispatcher` (serial, dedupe by update_id, owner fail-closed) →
   `process_update_sync`. Polling and webhook are two FRONT ENDS of the
   same dispatcher — the semantics (order, uniqueness, at-least-once)
   are identical and frozen in tests (test_dispatcher, test_poller,
   test_webhook).
2. **Mutually exclusive** via config `"transport": "polling"|"webhook"`
   (default `polling`). Never both at once: getUpdates with an active
   webhook would give 409, and two readers would violate the original
   invariant. `/set_webhook` sets `webhook`; `/delete_webhook` returns
   to `polling`. The choice is read at every process startup
   (`runtime_async`).
3. **Mandatory auth**: Telegram signs every delivery with
   `X-Telegram-Bot-Api-Secret-Token`; the receiver compares in constant
   time (`hmac.compare_digest`) against the config's `webhook_secret`
   and is FAIL-CLOSED: no secret, 401 to everything. `/set_webhook`
   generates the secret (`secrets.token_urlsafe`) if not provided — the
   URL alone is never enough. Default bind `127.0.0.1`.
4. **Idempotency**: the receiver answers 200 as soon as it authenticates
   and enqueues; Telegram retries on timeout/non-200 and the
   dispatcher's dedupe makes the redelivery idempotent (the 10/14
   offset incident does not exist in this transport: there is no offset
   to confirm).
5. **Server inside the gateway process** (`runtime_async`, same
   LaunchAgent): the poller's asyncio loop (F2) stays free while the
   long-poll runs in the executor; in webhook mode the same loop hosts
   receiver + supervisor (reload/orphans/outbox every 3 s).
6. **Runtime**: aiohttp (client+server), Python 3.13 from the venv (see
   ADR-010).

## Consequences

- Command latency ~50 ms on webhook (vs ≤3 s of the F22 long-poll).
- Requires a reachable public HTTPS URL (port-forwarding/tunnel + TLS;
  self-signed works if registered in setWebhook) — that is why the
  default is still polling: without network infrastructure, the webhook
  adds nothing.
- The no-polling supervisor lives in `poller.supervisor_loop`.
- The tg-busy flag and the hooks remain the same: they share getUpdates
  ONLY in polling mode; in webhook mode the hooks must NOT long-poll
  (wait_reply, then in the hook path, today lives in commands/dispatch.py).
