# Architecture

A high-level tour of how Mailtivo-Relay is put together. If you're reading
this to make changes, the corresponding source paths are linked inline.

## Request lifecycle (outbound)

```
Customer SDK
    │
    ▼
POST /api/v1/emails               apps/messages_api/views.py
    │
    │ 1. Bearer auth + scope      apps/messages_api/auth.py
    │ 2. Per-API-key rate limit   apps/messages_api/ratelimit.py
    │ 3. Validate body            apps/messages_api/serializers.py
    │ 4. Pre-send suppression     apps/suppressions/models.py
    │ 5. Idempotency replay       apps/messages_api/models.py
    │ 6. Persist Message          (Fernet-encrypted body, ULID id)
    │ 7. Enqueue on Django-Q2     apps/sending/tasks.py
    │
    ▼ HTTP 200 {"id": "<ulid>"}
```

The request returns the moment the row is persisted. Sending itself happens
in the worker — `apps.sending.tasks.send_message` picks the queued message,
walks the pool router, and calls the chosen adapter.

## Worker pipeline

`send_message(message_id)`:

1. Loads the Message.
2. Calls `apps.pools.routing.select_member(pool)` to pick a `PoolMember`.
3. Instantiates the connection's adapter (`apps/connections/adapters/`).
4. Calls `adapter.send(message=message.to_adapter_payload())`.
5. On success: marks message `sent`, records the per-recipient tokens,
   increments the member's daily counter (for warm-up enforcement), marks
   the connection healthy.
6. On `AdapterError(temporary)`: records the attempt, marks the connection
   degraded (and sets `skip_until` on 5xx), falls through to the next
   pool member up to `MAX_ATTEMPTS = 5`.
7. On `AdapterError(permanent)`: records the attempt, marks the message
   `failed` without retrying on another member (the body is bad — try-next
   won't help).

## Pool routing

`apps/pools/routing.py`:

- **Weighted** — random choice with weights as probabilities; zero weight
  means "disabled".
- **Failover** — sorts by `(priority, id)`, always picks the lowest.
- **Round-robin** — rotates through the full member list using a persistent
  cursor (`Pool.rr_cursor`) so a degraded slot still consumes its turn
  (skipped, not starved).

All three are layered on top of a health filter that skips:
- Disabled pool members or disabled connections.
- Connections marked `down` or inside their `skip_until` cool-down.
- Members at their daily cap (per-connection `daily_cap` or warm-up).

## Warm-up

`apps/pools/models.py` — `WarmupPlan(pool, curve, start_date)`. Presets:

- `sendgrid-standard` — 30-day SendGrid-style ramp.
- `aggressive` — 14-day ramp.
- `custom` — arbitrary daily caps from a CSV.

`PoolMember.at_cap()` consults the plan's `cap_for(today)` and the
connection's own `daily_cap`, taking whichever is smaller. The router refuses
to pick a capped member; once `record_send()` ticks the member past the cap,
fall-through kicks in.

## Health-aware skip

`apps/connections/models.py` — `Connection` has rolling 60-second counters
(`recent_5xx_count`, `recent_total`) that reset via `tick_window()` and a
`skip_until` timestamp written when a 5xx fires. The router filters the
member out while `skip_until > now()`; the next successful send clears it.

## Inbound webhooks

`apps/events/views.py` exposes:

- `POST /webhooks/postal/<connection_id>/`
- `POST /webhooks/resend/<connection_id>/`
- `POST /webhooks/ses/<connection_id>/` — an SNS HTTPS endpoint; it verifies
  the SNS signature on every message type, auto-confirms
  `SubscriptionConfirmation`s, and feeds `Notification`s into the shared
  ingest path (see [providers/ses.md](providers/ses.md)).

All endpoints look up the connection, call `adapter.verify_webhook(headers,
body)` (RSA-PKCS1v15 for Postal, Svix HMAC for Resend, SNS X.509 signature
for SES), parse via `adapter.parse_event`, and persist a single `Event` row. The
`provider_event_id` column is `UNIQUE`, so duplicate redeliveries auto-dedupe
at the database. For Resend we override the parser's id with the Svix `svix-id`
header so dedup keys match Resend's own retry semantics.

Terminal events (delivered, bounced, complained, failed) walk a status
precedence ladder (`apps/events/views.py:_STATUS_RANK`) so opens/clicks
never regress a delivered message but a later complaint does upgrade the
final state.

Hard bounces and complaints are auto-added to the suppression list with the
event recipient as the key.

## Outbound webhooks

`apps/events/outbound.py` fans out the same normalized `Event` to every
enabled `WebhookEndpoint`. Each delivery:

- Signs `{timestamp}.{body}` with HMAC-SHA256 using the part of the signing
  secret after `whsec_`.
- POSTs with `Mailtivo-Signature: t=<unix>,v1=<hex>`, `Mailtivo-Timestamp`,
  `Mailtivo-Event`.
- Retries via Django-Q2 `schedule(...)` on an exponential backoff
  (5s → 30s → 2m → 10m → 1h → 6h, max 6 attempts).

A `WebhookDelivery` row is created per attempt cluster, with a
`UniqueConstraint(endpoint, event)` so a single event never enqueues
multiple deliveries to the same endpoint.

## Storage model

- **Message bodies** are JSON-encoded `{"html": ..., "text": ...}` then
  Fernet-encrypted to `BinaryField`. Plaintext never hits disk except in
  panel templates rendered to logged-in admins.
- **Provider credentials** + **Resend webhook secrets** are Fernet-encrypted
  on the `Connection` row.
- **API keys** are SHA-256 hashed — plaintext is shown to the user exactly
  once at issuance.
- **Magic-link tokens** are SHA-256 hashed in `MagicLinkToken.token_hash`.
- **Attachments** are stored base64-encoded as `TextField` inline. v2 may
  spool large attachments to S3-compatible storage.

## Settings layout

- `mailtivo_relay/settings/base.py` — the common config; everything turns on
  here.
- `dev.py` — adds `django_extensions`, sets `DEBUG=True`, allows all hosts.
- `prod.py` — sets HSTS, SSL redirect, secure cookies, proxy SSL header.
- `test.py` — SQLite in-memory, MD5 hasher for speed, fixed Fernet key.

## What's intentionally simple

- **No Celery / Redis required** — Django-Q2 with the ORM broker is the
  default. Redis can be slotted in with one settings change for scale.
- **No SPA** — Django templates + HTMX + Alpine. Server-rendered pages, partial
  swaps for filters and live KPIs.
- **No custom UI library** — Tailwind v4 with a small set of `@apply`'d
  component classes in `mailtivo_relay/static_src/input.css`.
- **Single workspace per install** — the schema reserves room for multi-
  workspace tenancy, but v1 doesn't wire it up.

## Where to look first

| Concern | File |
| --- | --- |
| Add a provider | `apps/connections/adapters/` + a fixture in `docs/providers/<p>-payload-fixtures/` |
| Change pool strategy | `apps/pools/routing.py` |
| Touch send pipeline | `apps/sending/tasks.py` |
| Touch API validation | `apps/messages_api/serializers.py` |
| Add a panel page | `apps/<app>/views.py` + `templates/<app>/...` + `panel/_sidebar.html` |
| Add an audit hook | `from apps.audit.models import AuditLog; AuditLog.record(actor, action=..., target=..., detail=...)` |
