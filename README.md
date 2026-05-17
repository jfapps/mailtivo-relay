# Mailtivo-Relay

> Self-hosted, MIT-licensed email relay with a **Resend-compatible HTTP API**.
> Routes through pluggable upstream providers (v1: **Postal** + **Resend**) so
> you can warm up, split traffic, or migrate without touching application code.

Built for solo builders and small teams who want one polished sending surface
on top of any provider. Drop-in compatible with the Resend SDK — point its
`base_url` at your relay and keep shipping.

[![CI](https://github.com/your-org/mailtivo-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/mailtivo-relay/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Why

- **Provider lock-in is a tax.** Mailtivo-Relay puts a stable API in front of
  Postal and Resend so swapping (or splitting) providers is a config change,
  not a refactor.
- **Postal's UI is barebones.** This is the polished panel: messages timeline,
  pools, warmup curves, suppressions, audit log.
- **Resend SDKs already exist in every language.** We mimic Resend's wire
  format so you don't need new client libraries.

## Quickstart (docker-compose, < 5 minutes)

```bash
git clone https://github.com/your-org/mailtivo-relay
cd mailtivo-relay
cp .env.example .env

# Mint a body-encryption key.
python -c "from cryptography.fernet import Fernet; print('RELAY_FERNET_KEY=' + Fernet.generate_key().decode())" >> .env

docker compose up -d --build
open http://localhost:8000   # onboarding wizard → first owner account
```

1. Onboarding: pick a workspace name + create the owner account.
2. **Connections** → add a Resend or Postal connection. Click *Test*.
3. **Pools** → create a pool; attach the connection.
4. **API Keys** → issue a key with the pool as its default.
5. Send your first message:

```python
import resend
resend.api_key = "mr_live_..."
resend.api_url = "http://localhost:8000/api/v1"
print(resend.Emails.send({
    "from": "you@yourdomain.test",
    "to": ["dest@example.test"],
    "subject": "Hello from Mailtivo-Relay",
    "html": "<p>It works.</p>",
}))
```

## SDK swap

| SDK | Change |
| --- | --- |
| Python | `resend.api_url = "https://your-relay/api/v1"` |
| Node   | `new Resend("mr_live_...", { baseUrl: "https://your-relay/api/v1" })` |
| cURL   | replace `https://api.resend.com` with `https://your-relay/api/v1` |

## What's in v1

- `/emails`, `/emails/:id`, `/api-keys` — Resend-compatible HTTP API
- Bearer auth, `Idempotency-Key` (24h replay cache), 422 validation, 429 rate limit
- **Pools** — weighted / failover / round-robin + health-aware skip
- **Warm-up curves** — SendGrid-standard (30d), aggressive (14d), or custom CSV
- **Suppressions** — pre-send filter, auto-add on hard bounce / complaint, CSV import/export
- **Unified events** — incoming Postal + Svix-signed Resend webhooks, dedupe on provider event id
- **Outbound webhooks** — sign with HMAC-SHA256, exponential backoff retry
- **Fernet-encrypted body storage** with per-workspace retention (default 30 days)
- **Audit log** for every admin action
- **Custom panel** at `/app/...` — Django templates + HTMX + Tailwind + Alpine

Not in v1: `/domains`, `/contacts`, `/audiences`, `/broadcasts`, multi-workspace
tenancy, additional providers (SES / Mailgun / SendGrid / generic SMTP),
inbound parsing.

## Architecture

```
Customer SDK ──▶ POST /api/v1/emails ──▶ SendController
                                           │ auth + idempotency + suppression
                                           ▼
                                       Django-Q2 worker
                                           │
                                           ▼
                                       Pool router (weighted/failover/RR + health-skip)
                                           │
                                  ┌────────┴────────┐
                                  ▼                 ▼
                            Postal adapter    Resend adapter
                                  │                 │
                                  └──── webhooks ───┘
                                           ▼
                            /webhooks/{postal,resend}/<id>/  (sig-verified)
                                           │
                                           ▼
                                    Event + Message status
                                           │
                                           ▼
                                    Outbound webhooks  →  your app
```

See [docs/architecture.md](docs/architecture.md) for the full breakdown.

## Provider setup

- **[Postal](docs/providers/postal.md)** — API key, base URL, webhook public key
- **[Resend](docs/providers/resend.md)** — API key, Svix webhook secret

## API reference

See [docs/api.md](docs/api.md) for endpoint shapes, error envelopes, and the
outbound webhook signature scheme.

## Development

```bash
python -m venv .venv
source .venv/bin/activate     # or .\.venv\Scripts\Activate.ps1 on Windows
pip install -r requirements-dev.txt

cp .env.example .env
# Mint a Fernet key — see Quickstart above.

python manage.py migrate
python manage.py runserver
# In another shell:
python manage.py qcluster

# Tests
pytest apps

# Tailwind rebuild (during template development)
tailwindcss -i mailtivo_relay/static_src/input.css \
            -o mailtivo_relay/static/css/app.css --minify
```

Or run everything in containers:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## Security

See [SECURITY.md](SECURITY.md) for how to report vulnerabilities and the
relay's security defaults (Fernet encryption, Argon2id, CSP, HSTS, login
lockout, signed webhooks).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). TL;DR: open an issue first if you're
adding a feature; keep PRs scoped; tests are required.

## License

MIT — see [LICENSE](LICENSE).
