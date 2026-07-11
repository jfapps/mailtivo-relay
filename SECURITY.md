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
  Resend via Svix HMAC-SHA256, SES via the SNS message X.509 signature
  (with the signing-cert URL constrained to `sns.<region>.amazonaws.com`).
  Each SES connection is additionally pinned to a single SNS topic ARN, so a
  validly-signed message from any other topic is rejected.
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

## Rotating the encryption key

`RELAY_FERNET_KEY` encrypts provider credentials, webhook secrets, message
bodies, and the AI/Safe-Browsing keys. To rotate it without downtime or data
loss:

1. Mint a new key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Set the **new** key as `RELAY_FERNET_KEY` and move the **current** key to
   `RELAY_FERNET_KEY_OLD`, then redeploy. New writes use the new key;
   `MultiFernet` still decrypts anything encrypted with the old key.
3. Re-encrypt existing rows so the old key can be retired. There is no
   built-in re-encryption command yet, so run a one-off in the app shell —
   load each row holding a `*_encrypted` field, call `set`/`encrypt` with the
   already-decrypted value, and save. Track this in the issue tracker if you
   need it automated.
4. Once every row is re-encrypted, remove `RELAY_FERNET_KEY_OLD` and redeploy.

Losing `RELAY_FERNET_KEY` (with no `_OLD` fallback) makes all encrypted data
permanently unrecoverable — back it up in your secrets manager.

## Known caveats

- **Google OAuth client secret** (only if you enable Google sign-in): the
  workspace stores it Fernet-encrypted, but django-allauth needs the cleartext
  to perform the OAuth exchange and keeps its own **unencrypted** copy in the
  `socialaccount_socialapp` table. A database-only compromise would therefore
  expose the Google OAuth secret (this does *not* affect the email-provider
  credentials, which are never written in cleartext). Keep the database on a
  private network, and rotate the secret in Google Cloud if the DB is exposed.
- **Provider credentials in `DEBUG`**: never run production with `DEBUG=True`.
  The technical 500 page can render decrypted values held in local variables.
  The `prod` settings module forces `DEBUG=False` and refuses to boot without
  `DJANGO_SECRET_KEY` and `RELAY_FERNET_KEY` set, specifically to prevent this.

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
