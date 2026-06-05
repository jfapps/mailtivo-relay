# Contributing

Thanks for your interest in Mailtivo-Relay! A few ground rules to keep PRs
smooth.

## Before you start

- **Open an issue** for non-trivial features so we can agree on scope before
  you write code.
- **Bug fixes** can land as PRs directly — include a regression test.
- **Provider integrations** require fresh API docs verification (see below).

## Provider integration policy

Mailtivo-Relay's value prop is faithful upstream parity. Before touching any
adapter (`apps/connections/adapters/*`) or webhook verifier:

1. Fetch the latest official docs from the provider.
2. Pin the docs URL + retrieval date in a comment at the top of the file.
3. Add or update a sanitized payload fixture under
   `docs/providers/<provider>-payload-fixtures/`.
4. Add a test that loads that fixture and asserts the normalized shape.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# Mint a Fernet key — see README quickstart.
make migrate
make run
# In another shell:
make worker
```

**Dev sign-in shortcut:** when running under `DEBUG=True` (the `make run` dev
server, or the containerized stack via `./scripts/dev-local.sh`), visit
`/dev-login/` to sign in as the first workspace admin without a password (or
`/dev-login/?email=...` to impersonate a specific user). The route only exists
under `DEBUG` and the view 404s otherwise, so it can't leak into production.
Note `./scripts/prod-local.sh` runs `settings.prod` (`DEBUG=False`), where the
route is absent. See the README's *Development* section for details.

## What's required for a PR

- `make check` is green (ruff + mypy + pytest).
- New behavior has a test. Bug fixes have a regression test.
- The commit message explains *why*, not *what*.
- Migrations are committed alongside model changes.
- Templates that change copy aren't whitespace-only diffs — run Tailwind:
  `make tailwind`.
- No new dependencies without justification in the PR description.

## House style

- Type hints on public functions; the codebase uses Python 3.13.
- Prefer Django ORM helpers over raw SQL.
- Keep templates small — extract partials when one grows past ~60 lines.
- HTMX/Alpine over JS where possible; no SPA framework.
- Tests live in `apps/<app>/tests/`, follow the existing one-file-per-concern style.

## Filing issues

Please include:
- The version of Mailtivo-Relay (commit SHA is fine).
- The provider(s) involved.
- The exact request / webhook that triggered the issue.
- Logs from `docker compose logs worker` if it's send-related.

## Code of conduct

Be kind. Assume good faith. If you disagree with a maintainer decision, say so
and explain why — we'd rather argue in public than guess.
