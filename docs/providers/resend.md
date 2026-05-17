# Resend connection

[Resend](https://resend.com/) is a hosted email-API service. Mailtivo-Relay
fronts a single Resend account as one Connection.

## What you need from Resend

1. **API key** — Resend dashboard → *API Keys*. Looks like `re_xxxxxxxx...`.
   Choose **Sending access** scope.
2. **Webhook signing secret** — Resend dashboard → *Webhooks* → *Add*.
   The signing secret looks like `whsec_<base64>` and is shown once.

## Configure in Mailtivo-Relay

1. **Connections** → *Add connection*.
2. Pick **Resend** as the provider.
3. Fill in:
   - Name: e.g. `resend-prod`.
   - Base URL: leave blank to use `https://api.resend.com`. Override only if
     you're testing against a mock.
   - API key: as above.
   - Webhook secret: as above (must start with `whsec_`).
4. Click *Test* — the relay does an authenticated GET against a known-404
   path; a 401 means a bad key, anything else means we're talking to Resend.

## Configure Resend's webhook

In Resend → *Webhooks*:

- Endpoint URL: `https://your-relay/webhooks/resend/<connection_id>/`
  (connection id is in the URL on the connection's edit page).
- Events: at minimum `email.delivered`, `email.bounced`, `email.complained`.
  Add `email.opened` / `email.clicked` / `email.failed` to surface the full
  timeline in the panel.

Resend's webhooks are **Svix-signed**: HMAC-SHA256 over
`f"{svix_id}.{svix_timestamp}.{raw_body}"`, secret = base64-decode of the
value after the `whsec_` prefix. The relay validates `svix-signature` and
ignores any whose verification fails (401).

## What the relay does with Resend

- Translates outgoing sends to `POST /emails` and forwards the
  `Idempotency-Key` header when present.
- Maps Resend events:

| Resend type | Internal type | Affects message status |
| --- | --- | --- |
| `email.sent` | `sent` | yes (→ `sent`) |
| `email.delivered` | `delivered` | yes |
| `email.delivery_delayed` | `deferred` | no |
| `email.bounced` | `bounced` | yes |
| `email.complained` | `complained` | yes |
| `email.opened` | `opened` | no |
| `email.clicked` | `clicked` | no |
| `email.failed` | `failed` | yes |

## Known gotchas

- Resend's documented rate limit is **5 req/s per team**. Mailtivo-Relay's
  own per-API-key limit (default 10/s) is upstream-blind, so configure your
  pool routing accordingly if you're under heavy load.
- Resend webhook payloads don't carry a globally unique event id in the
  body — the relay uses the Svix `svix-id` header for deduplication.
- DKIM is configured in the Resend dashboard; the relay forwards as-is.

## Reference

- Send API: https://resend.com/docs/api-reference/emails/send-email
- Errors: https://resend.com/docs/api-reference/errors
- Rate limit: https://resend.com/docs/api-reference/rate-limit
- Webhook verification: https://resend.com/docs/dashboard/webhooks/verify-webhooks-requests
- Event types: https://resend.com/docs/webhooks/event-types
- Adapter source: [apps/connections/adapters/resend.py](../../apps/connections/adapters/resend.py)
- Sanitized payload fixtures: [resend-payload-fixtures/](resend-payload-fixtures/)
