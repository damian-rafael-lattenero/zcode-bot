# ADR-011 — Transporte de updates: dispatcher serial único, polling o webhook (excluyentes)

**Estado**: aceptado (2026-10-15). **Supersede parcialmente ADR-001** (un
solo lector de getUpdates): la invariante ya no es "el poller es el único
que llama getUpdates" sino "EL DISPATCHER es el único que procesa
updates" — el transporte es intercambiable.

**Contexto**: el incidente de red del 14/10 (Connection reset / SSL
timeouts a api.telegram.org, mitigado con pin de /etc/hosts) motivó el
webhook: Telegram EMPUJA los updates y el long-poll desaparece (con él,
esa clase de fallos). El intento del commit `952b50f` quedó roto (sin
auth, sin owner check, handlers inexistentes) y se quemó en F0.

## Decisión

1. **Un pipeline, dos transportes**: `normalize_update` →
   `UpdateDispatcher` (serial, dedup por update_id, owner fail-closed) →
   `process_update_sync`. El polling y el webhook son dos DELANTES del
   mismo dispatcher — la semántica (orden, unicidad, at-least-once) es
   idéntica y está congelada en tests (test_dispatcher, test_poller,
   test_webhook).
2. **Mutuamente excluyentes** por config `"transport": "polling"|"webhook"`
   (default `polling`). Nunca ambos a la vez: getUpdates con webhook
   activo daría 409, y dos lectores violarían la invariante original.
   `/set_webhook` setea `webhook`; `/delete_webhook` vuelve a `polling`.
   La elección se lee en cada arranque del proceso (`runtime_async`).
3. **Auth obligatoria**: Telegram firma cada delivery con
   `X-Telegram-Bot-Api-Secret-Token`; el receiver compara en tiempo
   constante (`hmac.compare_digest`) contra `webhook_secret` de config y
   es FAIL-CLOSED: sin secret, 401 a todo. `/set_webhook` genera el
   secret (`secrets.token_urlsafe`) si no viene — la URL sola nunca
   basta. Bind default `127.0.0.1`.
4. **Idempotencia**: el receiver responde 200 apenas autentica y encola;
   Telegram reintenta ante timeout/no-200 y el dedup del dispatcher
   vuelve idempotente el re-envío (el incidente de offsets del 14/10 no
   existe en este transporte: no hay offset que confirmar).
5. **Server dentro del proceso gateway** (`runtime_async`, mismo
   LaunchAgent): el loop asyncio del poller (F2) queda libre mientras el
   long-poll corre en el executor; en modo webhook el mismo loop hostea
   receiver + supervisor (recarga/huérfanos/outbox cada 3 s).
6. **Runtime**: aiohttp (cliente+server), Python 3.13 del venv
   (ver ADR-010).

## Consecuencias

- Latencia de comando ~50 ms en webhook (vs ≤3 s del long-poll F22).
- Requiere URL pública HTTPS alcanzable (port-forwarding/túnel + TLS;
  self-signed vale registrándolo en setWebhook) — por eso el default
  sigue siendo polling: sin infraestructura de red, el webhook no aporta.
- El supervisor sin polling vive en `poller.supervisor_loop`.
- El flag tg-busy y los hooks siguen siendo los mismos: comparten
  getUpdates SOLO en modo polling; en webhook los hooks NO deben hacer
  long-poll (wait_reply migrará al dispatcher en F4 si se adopta).
