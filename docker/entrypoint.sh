#!/usr/bin/env bash
# Mailtivo-Relay container entrypoint.
#
# Modes:
#   web     — apply migrations + collectstatic, then run gunicorn.
#   worker  — run the Q2 cluster. Does NOT migrate: compose starts the worker
#             only after web is healthy (= migrations done), so a single
#             process owns schema changes and there is no concurrent-migrate race.
#   migrate — run migrations and exit (handy for one-shot jobs / k8s init).
#   shell   — drop into manage.py shell (interactive).

set -euo pipefail

cmd="${1:-web}"

run_migrations() {
    echo "[entrypoint] running migrations…"
    python manage.py migrate --noinput
}

collect_static() {
    echo "[entrypoint] collecting static files…"
    python manage.py collectstatic --noinput
}

case "$cmd" in
    web)
        run_migrations
        collect_static
        exec gunicorn mailtivo_relay.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "${GUNICORN_WORKERS:-3}" \
            --timeout "${GUNICORN_TIMEOUT:-60}" \
            --access-logfile - \
            --error-logfile -
        ;;
    worker)
        exec python manage.py qcluster
        ;;
    migrate)
        run_migrations
        ;;
    shell)
        exec python manage.py shell
        ;;
    manage)
        shift
        exec python manage.py "$@"
        ;;
    *)
        # Allow arbitrary commands (e.g. `docker compose run web bash`).
        exec "$@"
        ;;
esac
