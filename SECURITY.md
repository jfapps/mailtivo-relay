# Security Policy

## Reporting a vulnerability

If you believe you've found a security issue in Mailtivo-Relay, **do not open
a public GitHub issue**. Email the maintainer at the address listed on the
repository's profile page, or open a private security advisory via GitHub's
"Report a vulnerability" button on the Security tab.

We will acknowledge receipt within 72 hours and aim to ship a patched release
within 14 days for critical issues. Coordinated disclosure is appreciated.

## Supported versions

Mailtivo-Relay is pre-1.0; only `main` is supported. After 1.0.0 we will
support the latest minor release and the previous one for security patches.

## Security defaults

- **Body storage** is Fernet-encrypted at rest. Set `RELAY_FERNET_KEY`; rotate
  via `RELAY_FERNET_KEY_OLD` (old key remains valid for decryption).
- **Passwords** are hashed with **Argon2id**.
- **API keys** are stored as SHA-256 hashes; the plaintext is shown to the
  user exactly once at issuance.
- **Provider credentials + webhook secrets** are stored Fernet-encrypted on
  the `Connection` row.
- **Inbound webhooks** are signature-verified before any side effects:
  Postal via RSA-PKCS1v15 (SHA-256 preferred, SHA-1 legacy fallback),
  Resend via Svix HMAC-SHA256.
- **Outbound webhooks** are signed with HMAC-SHA256 over `{t}.{body}` and
  carry `Mailtivo-Signature: t=<unix>,v1=<hex>`. The signing secret can be
  rotated from the panel.
- **Login lockout** activates after 10 failed attempts from the same IP within
  15 minutes.
- **Rate limit** defaults to 10 req/s and 600 req/min per API key (configurable).
- **CSP, Permissions-Policy, X-Content-Type-Options, Referrer-Policy** are
  set on every panel response.
- **HSTS, SSL redirect, secure cookies, X-Frame-Options=DENY** are enabled in
  the `prod` settings module.

## Threat model assumptions

- The host running Mailtivo-Relay is trusted; it has access to plaintext
  Fernet keys, provider credentials, and signing secrets.
- The PostgreSQL database is on a private network or otherwise inaccessible
  to untrusted clients. We do not encrypt application data at the row level
  beyond what's noted above.
- TLS termination happens at the edge (a reverse proxy or load balancer). The
  relay's `prod` settings assume an `X-Forwarded-Proto` header set to `https`.

## Out of scope (v1)

- DKIM signing — the relay passes mail through to upstream providers, which
  apply their own DKIM. This is by design (Domain-auth pass-through).
- Multi-workspace tenancy — a single install serves a single workspace.
- DDoS protection beyond the per-API-key rate limit — put a CDN / WAF in
  front for that.
