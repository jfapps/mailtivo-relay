# HTTP API

Mailtivo-Relay exposes a **Resend-compatible** HTTP API at `/api/v1/`. Drop
the Resend SDK in unchanged — just override the base URL.

## Authentication

Every request must include a Bearer token issued from the panel.

```
Authorization: Bearer mr_live_<secret>
```

API keys carry **scopes**:
- `send` — required for `POST /emails`
- `read` — required for `GET /emails/:id`
- `*` — both

Revoked keys, missing/invalid headers, or insufficient scope return:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"statusCode": 401, "name": "missing_api_key", "message": "..."}
```

## Rate limit

Per-API-key, defaults: **10 req/s** and **600 req/min**. Set via env:
`API_RATE_LIMIT_PER_SECOND`, `API_RATE_LIMIT_PER_MINUTE`.

Exceeded → `429` with Resend-shaped body and standard headers:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 1
RateLimit-Limit: 10
RateLimit-Remaining: 0
```

## POST /api/v1/emails

Request body (mirrors Resend):

```json
{
  "from": "Acme <onboarding@your-domain.test>",
  "to": ["dest@example.test"],
  "cc": [],
  "bcc": [],
  "reply_to": [],
  "subject": "Hello",
  "html": "<p>Hi there.</p>",
  "text": "Hi there.",
  "headers": {"X-Tag": "newsletter"},
  "tags": [{"name": "category", "value": "transactional"}],
  "attachments": [
    {"filename": "invoice.pdf", "content": "<base64>", "content_type": "application/pdf"}
  ],
  "scheduled_at": "2026-06-01T10:00:00.000Z"
}
```

Rules:
- At least one of `html` or `text` is required.
- `to`, `cc`, `bcc`, `reply_to` accept either a string or a list of strings.
- `attachments[].content` is base64; URL-based attachments (`path`) aren't supported in v1.
- `scheduled_at` is ISO-8601, at most 30 days ahead. A future timestamp defers
  dispatch until that time (the message shows as `scheduled`); a past or absent
  timestamp sends immediately. Scheduled dispatch runs on the worker, so the
  `worker` service must be up. There is no cancel endpoint yet.

### Headers

| Header | Purpose |
| --- | --- |
| `Authorization: Bearer …` | required |
| `Idempotency-Key: <key>` | optional; 24h replay cache per API key |
| `Content-Type: application/json` | required |

### Response

```http
HTTP/1.1 200 OK
{"id": "01HG5QYZP3XK2QY2J9MMABNA00"}
```

### Errors

| Status | `name` | Meaning |
| --- | --- | --- |
| 400 | `no_pool_assigned` | The API key has no default pool. Assign one in the panel. |
| 401 | `missing_api_key` | Authorization header is missing or invalid. |
| 403 | `insufficient_scope` | API key lacks `send`. |
| 409 | `idempotency_conflict` | Same `Idempotency-Key` used with a different body. |
| 422 | `validation_error` | Request body failed validation. The `message` describes the offending field. |
| 422 | `recipient_suppressed` | All recipients are on the suppression list. |
| 429 | `rate_limit_exceeded` | Per-API-key limit hit. |

## GET /api/v1/emails/:id

```http
GET /api/v1/emails/01HG5QYZP3XK2QY2J9MMABNA00
Authorization: Bearer mr_live_...
```

```json
{
  "object": "email",
  "id": "01HG5QYZP3XK2QY2J9MMABNA00",
  "from": "Acme <onboarding@your-domain.test>",
  "to": ["dest@example.test"],
  "subject": "Hello",
  "html": "<p>Hi there.</p>",
  "text": "Hi there.",
  "headers": {},
  "tags": [],
  "last_event": "delivered",
  "created_at": "2026-05-17T12:00:00+00:00",
  "scheduled_at": null
}
```

## Outbound webhooks

Configure in the panel at `/app/webhooks/`. Each delivery is signed:

```
POST /your/endpoint
Content-Type: application/json
Mailtivo-Timestamp: 1747500000
Mailtivo-Signature: t=1747500000,v1=<hex HMAC-SHA256>
Mailtivo-Event: delivered
```

The signed payload is `f"{t}.{raw_body}"`. The key is the part of the
signing secret **after** the `whsec_` prefix.

Verification example (Python):

```python
import hmac, hashlib, time

def verify(secret: str, body: bytes, header: str, *, max_skew=300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, v1 = parts.get("t", ""), parts.get("v1", "")
    if abs(time.time() - int(ts)) > max_skew:
        return False
    key = secret.removeprefix("whsec_").encode()
    expected = hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)
```

Retry schedule: 5s → 30s → 2m → 10m → 1h → 6h (6 attempts).

## Event payload

Outbound webhook body is provider-agnostic:

```json
{
  "id": "evt_42",
  "type": "delivered",
  "occurred_at": "2026-05-17T12:01:30+00:00",
  "data": {
    "message_id": "01HG5QYZP3XK2QY2J9MMABNA00",
    "provider_message_id": "abc-resend-id",
    "recipient": "dest@example.test",
    "link_url": null,
    "user_agent": null,
    "ip": null,
    "bounce_reason": null
  }
}
```

Event types: `sent`, `delivered`, `deferred`, `bounced`, `complained`,
`opened`, `clicked`, `failed`, `queued`, `unknown`.

## Inbound webhooks

These are the relay's **inbound** webhook URLs that you give to the
upstream provider:

- Postal → `https://your-relay/webhooks/postal/<connection_id>/`
- Resend → `https://your-relay/webhooks/resend/<connection_id>/`

Signatures are verified before the event is persisted; bad signatures return
401 and never create an Event row.

## Health probe

`GET /healthz/` returns `{"ok": true}` without touching the database. Used by
the docker-compose healthcheck.
