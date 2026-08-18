# Mailtivo-Relay

> Self-hosted, MIT-licensed email relay with a **Resend-compatible HTTP API**.
> Routes through pluggable upstream providers (**Postal**, **Amazon SES**, and
> **Resend**) so you can warm up, split traffic, or migrate without touching
> application code.

Built for solo builders and small teams who want one polished sending surface
on top of any provider. Drop-in compatible with the Resend SDK — point its
`base_url` at your relay and keep shipping.

[![CI](https://github.com/hassancs91/mailtivo-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/hassancs91/mailtivo-relay/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Why

- **Provider lock-in is a tax.** Mailtivo-Relay puts a stable API in front of
  Postal, Amazon SES, and Resend so swapping (or splitting) providers is a
  config change, not a refactor.
- **Postal's UI is barebones.** This is the polished panel: messages timeline,
  test inbox, pools, warmup curves, suppressions, spam checks, and an audit log.
- **Resend SDKs already exist in every language.** We mimic Resend's wire
  format so you don't need new client libraries.

## Quickstart (docker-compose, < 5 minutes)

```bash
git clone https://github.com/hassancs91/mailtivo-relay
cd mailtivo-relay
cp .env.example .env

# Mint the two required secrets (the app refuses to boot in prod without them).
python -c "import secrets; print('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(64))" >> .env
python -c "from cryptography.fernet import Fernet; print('RELAY_FERNET_KEY=' + Fernet.generate_key().decode())" >> .env

docker compose up -d --build
open http://localhost:8000   # onboarding wizard → first owner account
```

Deploying for real (Coolify or any docker host)? Also set
`DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` to your domain
(e.g. `relay.example.com` and `https://relay.example.com`), keep
`DATABASE_URL` pointing at Postgres, and make sure **both** the `web` and
`worker` services run — queued mail is only dispatched by the worker.

1. Onboarding: pick a workspace name + create the owner account.
2. **Connections** → add a Postal, Amazon SES, or Resend connection. Click *Test*.
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

## Updating

```bash
cd mailtivo-relay
git pull
docker compose up -d --build   # the entrypoint runs migrations automatically
```

The panel shows the running version in the sidebar footer, and flags an
**Update available** badge there (and on **Settings → Version & updates**) when a
newer GitHub release exists. That check is an opt-in, read-only daily lookup of
the latest release tag — no usage data is sent, and you can disable it on the
Settings page.

Running a fork? Point the check at your repo with `RELAY_GITHUB_REPO=owner/name`
in `.env`. The version itself is defined in `mailtivo_relay/__init__.py`.

## SDK swap

| SDK | Change |
| --- | --- |
| Python | `resend.api_url = "https://your-relay/api/v1"` |
| Node   | `new Resend("mr_live_...", { baseUrl: "https://your-relay/api/v1" })` |
| cURL   | replace `https://api.resend.com` with `https://your-relay/api/v1` |

## Testing mode (capture pools)

Point a dev/staging project at the relay and have its emails **captured for
inspection instead of actually sent** — like Mailpit/Mailtrap, built in.

1. **Pools → New pool**, set **Mode = Capture**, name it e.g. `Sandbox`. A capture
   pool needs no provider connections.
2. **API Keys → Issue key**, set **Default pool = Sandbox**.
3. Point your project's `RESEND_API_KEY` at that key. Send exactly as you would in
   production — no code changes.

Every send through a capture-pool key is stored and shown in the **Test Inbox**
(rendered HTML, text, headers, raw source), marked `captured`, and **never
delivered**. Capture runs synchronously in the request, so it works with only the
`web` process — no `worker` required. Suppression filtering is skipped for capture
pools so you can test sends to any address.

**Switch a key between live and test** in one click from **API Keys** — each key
shows its pool as an inline dropdown; point it at a capture pool to test, back at a
live pool to ship. No reissuing secrets.

**Simulate webhook events.** From a captured message's **Events** tab you can fire a
`delivered` / `opened` / `clicked` / `bounced` / `complained` event and have it
delivered to your configured outbound webhooks — so you can test your webhook
handler end-to-end. Simulated events are flagged `"test": true` in the payload and
`Mailtivo-Test: true` in the headers, and never touch the live suppression list.
(Actual webhook *delivery* needs the `worker` running and an enabled endpoint.)

## What's in v1

- `/emails`, `/emails/:id`, `/api-keys` — Resend-compatible HTTP API
- Bearer auth, `Idempotency-Key` (24h replay cache), 422 validation, 429 rate limit
- **Providers** — Postal, Amazon SES, and Resend behind one API
- **Scheduled sends** — `scheduled_at` defers dispatch (up to 30 days out)
- **Pools** — weighted / failover / round-robin + health-aware skip, or **capture** (testing) pools
- **Testing mode** — capture pools store sends in the Test Inbox instead of delivering (Mailpit-style), plus a synchronous **Test Send** tool for one-off checks
- **Warm-up curves** — SendGrid-standard (30d), aggressive (14d), or custom CSV
- **Suppressions** — pre-send filter, auto-add on hard bounce / complaint (transient soft bounces excluded), CSV import/export
- **Unified events** — incoming Postal, Amazon SES (SNS), and Svix-signed Resend webhooks, dedupe on provider event id
- **Outbound webhooks** — HMAC-SHA256 signed, exponential-backoff retries, plus event simulation for testing your handler
- **Spam analysis** — optional per-message AI scoring (OpenAI / Anthropic / Gemini) + Google Safe Browsing link checks
- **Fernet-encrypted body storage** with per-workspace retention (default 30 days) and a **Data & storage** page (manual purge, purge history, storage stats)
- **Audit log** for every admin action
- **Custom panel** at `/app/...` — Django templates + HTMX + Tailwind + Alpine

Not in v1: `/domains`, `/contacts`, `/audiences`, `/broadcasts`, multi-workspace
tenancy, additional providers (Mailgun / SendGrid / generic SMTP), inbound
parsing.

## Architecture

```
Customer SDK ──▶ POST /api/v1/emails ──▶ SendController
                                           │ auth + idempotency + suppression
                                           ▼
                                       Django-Q2 worker  (immediate or scheduled)
                                           │
                                           ▼
                                       Pool router (weighted/failover/RR + health-skip)
                                           │
                          ┌────────────────┼────────────────┐
                          ▼                ▼                ▼
                    Postal adapter     SES adapter     Resend adapter
                          │                │                │
                          └───────────── webhooks ──────────┘
                                           ▼
                        /webhooks/{postal,ses,resend}/<id>/  (sig-verified)
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
- **[Amazon SES](docs/providers/ses.md)** — IAM keys, region, config set + SNS event subscription
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

Or run the whole stack in containers. Two helper scripts wrap docker-compose:

```bash
# Development: dev server + autoreload + DEBUG=True (settings.dev). Use this for
# local feature work — the dev-login shortcut below only exists in this mode.
./scripts/dev-local.sh          # or, on Windows: .\scripts\dev-local.ps1

# Production-parity: gunicorn + secure cookies + DEBUG=False (settings.prod).
./scripts/prod-local.sh         # or: .\scripts\prod-local.ps1
```

Both accept `up` (default), `down`, `reset`, `restart`, `logs`, `ps`, `shell`,
`migrate`, and `build`. Under the hood the dev runner is just:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

### Dev sign-in shortcut

> **Requires dev mode** — the plain `docker compose up` and `prod-local` run on
> `settings.prod` (`DEBUG=False`), where this route does not exist (you'll get a
> 404). Start the stack with `dev-local` (or the dev overlay above) to use it.

When `DEBUG=True` you can skip the password form and sign in by visiting a URL:

- `http://localhost:8000/dev-login/` — signs in as the first workspace admin
- `http://localhost:8000/dev-login/?email=someone@example.test` — signs in as
  that specific (active) user

A dashed **"Dev sign-in"** button also appears on the login page in this mode.

This is double-guarded: the route is only registered when `DEBUG` is on, **and**
the view returns 404 if reached with `DEBUG` off — so it can never resolve in
production. It performs no credential check, so never run a real deployment with
`DJANGO_DEBUG=True`.

## Security

See [SECURITY.md](SECURITY.md) for how to report vulnerabilities and the
relay's security defaults (Fernet encryption, Argon2id, CSP, HSTS, login
lockout, signed webhooks).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). TL;DR: open an issue first if you're
adding a feature; keep PRs scoped; tests are required.

## License

MIT — see [LICENSE](LICENSE).

<!-- lwh-footer -->

---

## 📘 The free book

Boring building blocks are what keep an app alive in production. The book has 47 of them.

**[Vibe Engineering Blocks](https://learnwithhasan.com/blocks/)** is my free 74-page book.
47 building blocks for shipping real apps with AI. One block per page, each with the exact
prompt to hand your AI.

Built by **[Hasan Aboul Hasan](https://learnwithhasan.com)**. I build real products with AI and
write down exactly how.
[Guides](https://learnwithhasan.com/guides/) &nbsp;·&nbsp;
[YouTube](https://www.youtube.com/@HasanAboulHasan) &nbsp;·&nbsp;
[Community](https://learnwithhasan.com/community/)
