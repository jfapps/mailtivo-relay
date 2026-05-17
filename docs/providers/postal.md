# Postal connection

[Postal](https://postalserver.io/) is a fully-featured, self-hostable mail
server. Mailtivo-Relay treats one Postal **mail server** (not the whole
Postal instance) as a single Connection.

## What you need from Postal

1. **Base URL** — `https://postal.your-domain.test` (the Postal web interface URL).
2. **Server API key** — under your Postal mail server → *Credentials* →
   *API key*. Looks like a long opaque token.
3. **Webhook public key (PEM)** — the **public** half of Postal's signing key.
   It's accessible on the Postal instance as `config/signing.pub` or via the
   Postal admin UI. Paste the entire PEM block, beginning with
   `-----BEGIN PUBLIC KEY-----`.

## Configure in Mailtivo-Relay

1. **Connections** → *Add connection*.
2. Pick **Postal** as the provider.
3. Fill in:
   - Name: anything you like (e.g. `postal-prod`).
   - Base URL: as above.
   - API key: as above.
   - Webhook public key (PEM): as above.
4. Click *Test* — the relay POSTs an empty body to Postal's send endpoint;
   a structured error envelope back means we authenticated successfully.

## Configure Postal's webhook

In Postal → your mail server → *Webhooks*:

- URL: `https://your-relay/webhooks/postal/<connection_id>/` (the connection id
  is in the URL of the connection's edit page).
- Subscribed events: at minimum `MessageSent`, `MessageDeliveryFailed`,
  `MessageBounced`. Add `MessageLinkClicked` / `MessageLoaded` if you want
  opens / clicks in the timeline.

Postal signs every webhook body with **RSA-PKCS1v15**, base64-encoded, in the
`X-Postal-Signature-256` header (SHA-256, preferred) and/or
`X-Postal-Signature` (SHA-1, legacy). Mailtivo-Relay accepts both, preferring
SHA-256.

## What the relay does with Postal

- Translates outgoing sends to `POST /api/v1/send/message`.
- Maps Postal events:

| Postal event | Internal type | Affects message status |
| --- | --- | --- |
| `MessageSent` | `delivered` | yes (→ `delivered`) |
| `MessageDelayed` / `MessageHeld` | `deferred` | no |
| `MessageDeliveryFailed` | `failed` | yes |
| `MessageBounced` | `bounced` | yes |
| `MessageLinkClicked` | `clicked` | no |
| `MessageLoaded` | `opened` | no |

## Known gotchas

- Postal returns a **per-recipient `token`** rather than a single message id.
  The relay stores those tokens on the `Message` and uses them to match
  bounce / delivery webhooks back to the originating send.
- Postal v2 ships only the legacy SHA-1 signature header. v3+ includes both.
  Both are validated against the same public key.
- DKIM signing is owned by **Postal** itself, not the relay. Configure
  signing domains in Postal's admin.

## Reference

- Send API: https://docs.postalserver.io/developer/api/
- Webhooks: https://docs.postalserver.io/developer/webhooks/
- Adapter source: [apps/connections/adapters/postal.py](../../apps/connections/adapters/postal.py)
- Sanitized payload fixtures: [postal-payload-fixtures/](postal-payload-fixtures/)
