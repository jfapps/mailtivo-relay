# Mailtivo-Relay — Implementation Plan

> Self-hosted, MIT-licensed Django 6 + Django-Q2 email relay for solo builders.
> Resend-compatible HTTP API, routes through Postal / Resend (v1) via a Pool abstraction.

## Decisions locked in (2026-05-17)

| Topic | Choice |
| --- | --- |
| Tenancy | **Single workspace** per install (members can be invited). |
| Resend API scope (v1) | `/emails`, `/emails/:id`, `/api-keys`, unified events. **No** `/domains`, `/contacts`, `/audiences`, `/broadcasts` in v1. |
| Email body storage | Store metadata + body, **encrypted at rest** (Fernet), per-workspace TTL (default 30 d). |
| Frontend | **Django templates + django-htmx + Tailwind + Alpine.js**. No SPA. |
| Pool routing | **Weighted**, **failover**, **round-robin**, with **health-aware skip** layered on top. |
| Domain auth | **Pass-through to upstream provider.** Relay does not sign DKIM. |
| Postal dev env | User has an existing Postal — relay just needs config docs. |
| Deployment | **docker-compose** canonical: `web` + `worker` + `postgres`. Redis optional. |
| Background queue | **Django-Q2** with **ORM broker** (no Redis required). |
| Auth | Custom `User` (email PK), Argon2id, magic-link, **Google OAuth toggleable from UI**. |
| Admin panel | Fully custom UI at `/app/...`. `django.contrib.admin` **disabled** in v1. |

## Architecture

```
                    +-------------------------+
   Customer SDK --> | POST /emails (Resend    |
   (Resend SDK,     |  -compatible HTTP API)  |
    cURL, etc.)     +-----------+-------------+
                                |
                                v
                       +--------+--------+      enqueue
                       | SendController  +-------------+
                       | - auth API key  |             |
                       | - idempotency   |             v
                       | - suppression   |     +-------+--------+
                       | - persist Msg   |     | Django-Q2      |
                       +-----------------+     | worker         |
                                               +-------+--------+
                                                       |
                                                       v
                                            +----------+----------+
                                            | Pool router         |
                                            | weighted/failover/  |
                                            | round-robin + health|
                                            +----+-----------+----+
                                                 |           |
                                          Postal adapter  Resend adapter
                                                 |           |
                                                 v           v
                                              Postal       Resend
                                                 \         /
                                                  \       /  (webhooks back)
                                                   v     v
                                            +------+-----+------+
                                            | /webhooks/{p}     |
                                            | sig-verify -> Event|
                                            +-------------------+
```

## App layout

```
mailtivo_relay/
  settings/{base,dev,prod,test}.py
  urls.py asgi.py wsgi.py
apps/
  core/             # encryption, ulid pk, rate limit, security middleware
  accounts/         # custom User, magic link, allauth wiring, WorkspaceSettings, invitations
  panel/            # custom admin UI shell (sidebar, layout, dashboard, settings, audit log views)
  connections/      # Provider, Connection, encrypted creds, adapter base + postal/resend
  pools/            # Pool, PoolMember, routing, WarmupPlan
  api_keys/         # APIKey CRUD, scoped Bearer auth
  messages_api/     # Resend-compat /emails endpoints, Message + Attachment, idempotency
  events/           # unified Event, /webhooks/{postal,resend} ingestion + sig verify
  suppressions/     # Suppression list, pre-send check, CSV import/export
  sending/          # Q2 send pipeline task
  audit/            # AuditLog
theme/              # django-tailwind theme app
docs/
  api.md providers/postal.md providers/resend.md architecture.md
docker-compose.yml docker-compose.dev.yml Dockerfile
pyproject.toml .env.example LICENSE README.md
```

---

## STEP-BY-STEP (7 days)

### Day 1 — Project skeleton, custom User, base auth
1. `python -m venv .venv` + install Django 6, django-q2, django-htmx, django-tailwind, django-allauth, DRF, psycopg[binary], cryptography, argon2-cffi, python-dotenv, dj-database-url, ulid-py, svix.
2. `django-admin startproject mailtivo_relay .`, split settings.
3. Create `apps/*` modules, register, disable `django.contrib.admin`.
4. Custom `User` (email PK, `is_workspace_admin`), Argon2id.
5. `MagicLinkToken` (15-min, one-time).
6. `WorkspaceSettings` singleton (name, retention_days=30, google_oauth_*, branding, from_email_default).
7. `/onboarding` first-run wizard.

**Verify:** runserver → onboarding → land on `/app/dashboard`.

### Day 2 — Panel shell + Tailwind + Google OAuth toggle + invites
1. Tailwind init in `theme/`, Alpine import in `base.html`.
2. `apps/panel` layout: sidebar (Dashboard / Messages / Domains / Connections / Pools / API Keys / Suppressions / Settings), topbar, breadcrumbs, toast region.
3. Settings UI: workspace name, retention, branding.
4. Google OAuth toggle wires up allauth provider at runtime.
5. `Invitation` model + Settings → Team page (invite, revoke, accept).

**Verify:** log in, toggle Google OAuth, invite + accept a second user.

### Day 3 — Connections, adapters, API Keys
> **Docs-first rule:** Use WebSearch / WebFetch to pull the **latest official Postal + Resend docs** before writing any adapter code. Pin docs URL + retrieval date in the adapter file header.

1. Fetch latest provider docs (Postal send API + webhook signature scheme; Resend `/emails` + Svix webhook signing).
2. `core/encryption.py` Fernet field (`RELAY_FERNET_KEY` env, support `MultiFernet`).
3. `Connection(name, provider_code, base_url, credentials_encrypted, daily_cap, status, last_health_check_at, recent_error_rate, recent_5xx_count)`.
4. Adapter interface: `send`, `verify_webhook`, `parse_event`, `healthcheck`.
5. `PostalAdapter`, `ResendAdapter` — both implemented against freshly-fetched docs.
6. Connections UI: list, add/edit, "Test connection" button.
7. `APIKey(name, prefix='mr_live_', hash, scopes, last_used_at, revoked_at)`. CRUD UI; secret shown once.

**Verify:** Add real Resend + Postal connections, both pass "Test". Create + copy an API key.

### Day 4 — Pools, Resend-compatible Send API, idempotency
1. `Pool(routing_strategy, health_skip_enabled, thresholds)` + `PoolMember(weight, priority, enabled)`.
2. `WarmupPlan(curve, start_date, daily_caps_json)` — daily counter, UTC reset.
3. `pools/routing.py`: `select_member(pool, attempt_n, exclude)` per strategy + health filter.
4. Pools UI: list, add/edit, weight sliders (HTMX), warmup tab.
5. `Message` model (ULID, body encrypted, status enum, retention_expires_at, idempotency_key) + `Attachment`.
6. HTTP API at `/api/v1/`: `POST /emails`, `GET /emails/:id`. Bearer auth. Resend-shaped req/res. `Idempotency-Key` header → 24 h cache.
7. `sending/tasks.py` Q2 task: pool router → adapter → persist outcome → retry next member on failure.
8. `Q_CLUSTER` config; `qcluster` worker service.

**Verify:** Resend SDK with `base_url` overridden sends a real message through the relay; worker logs show router pick.

### Day 5 — Unified events ingestion + Messages UI
> **Docs-first rule applies again** — re-fetch Postal + Resend webhook payload + signature docs before coding parsers.

1. `Event(message, provider_event_id [unique], type, occurred_at, raw_jsonb, recipient, link_url, user_agent, ip)`.
2. `POST /webhooks/postal/<connection_id>/` — Postal signature verify per current spec.
3. `POST /webhooks/resend/<connection_id>/` — Svix signature verify per current spec.
4. Both endpoints dedup on `provider_event_id`; update `Message.status` on terminal events.
5. `docs/providers/<name>-payload-fixtures/` — sanitized real-payload fixtures locking parser behavior.
6. Messages UI: list (filters, search, HTMX-driven), detail (tabs: Overview / Headers / HTML / Text / Events timeline / Raw).
7. Dashboard KPIs: last-24h sent, delivery rate, bounce rate, throughput sparkline (HTMX poll).

**Verify:** End-to-end send → `delivered` event arrives → detail shows full timeline. Bad signature → 401.

### Day 6 — Suppression, warmup, health-skip, outbound webhooks, rate limits
1. `Suppression(email, reason, source_message)`. Pre-send check. Auto-add on hard bounce / complaint. CSV import/export.
2. Health-aware skip: rolling 1-min + 1-hr counters on Connection; auto-recovery cool-down (5 min).
3. Warmup execution: per-pool daily counter; router refuses members at cap; presets `sendgrid-standard`, `aggressive`, `custom`.
4. `WebhookEndpoint(url, signing_secret, enabled, event_types[])` (your customer's app); Q2 retry w/ exp backoff, HMAC-SHA256 `Mailtivo-Signature` header.
5. Per-API-key rate limit (default 10 req/s, 600 req/min) → Resend-shaped 429.
6. `AuditLog` records: API key issued/revoked, connection edits, pool weight changes, suppression manual ops, settings changes, member ops.

**Verify:** Hard-bounce → auto-suppression; warmup curve enforced; stubbed 5xx triggers health-skip + traffic flip; outbound webhook signature validates on test server.

### Day 7 — Hardening, docs, release
1. Security headers middleware (CSP for HTMX/Alpine, HSTS, X-Frame, Referrer, Permissions). CSRF on panel; never on `/api/v1/`. Login lockout (10 fails/15 min).
2. `docker-compose.yml` (postgres + web + worker), healthchecks, entrypoint `migrate` + `collectstatic`, `.env.example`, `Makefile`.
3. CI: pytest + ruff + mypy + bandit + pip-audit.
4. Docs: `README.md` (SDK swap snippet), `docs/api.md`, `docs/providers/{postal,resend}.md`, `docs/architecture.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE` (MIT).
5. GitHub repo prep: Issues + Discussions, labels, v2 starter issues.

---

## Verification (end-to-end)

```bash
docker compose up -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser

# Configure via UI: connections, pool, api key
# Then:
python -c "
import resend
resend.api_key='mr_live_...'
resend.api_url='http://localhost:8000/api/v1'
print(resend.Emails.send({'from':'hi@yourdomain.com','to':['you@example.com'],
  'subject':'Hello from Mailtivo-Relay','html':'<p>It works.</p>'}))
"

docker compose logs -f worker
# Messages detail → queued → sent → delivered timeline
```

## Out of scope v1 (designed-for in schema)
- `/domains` (relay-managed) + DKIM signing.
- `/contacts`, `/audiences`, `/broadcasts`.
- Multi-workspace tenancy.
- Additional providers (SES, Mailgun, SendGrid, generic SMTP).
- Inbound parsing.

## Risks
- Django 6.0 ecosystem gaps → pin verified versions; fallback 5.2 LTS only if blocker.
- Fernet key loss → document rotation, support `MultiFernet`.
- Provider webhook signature drift → adapter-owned verification with vendored fixtures.
- Q2 ORM broker scale → one-env-var Redis broker switch documented.
- Body storage growth → daily Q2 purge of expired Messages.

## Definition of done (v1.0.0)
- `docker compose up` from a clean machine → working relay.
- Real Resend Python + Node SDKs work unchanged (just `base_url` override).
- Postal + Resend both deliver real mail; events surface in unified timeline.
- All four routing strategies + health-skip have unit + integration tests.
- README quickstart works end-to-end in < 5 minutes on clean Docker host.
- CI green: pytest + ruff + mypy + bandit + pip-audit.
