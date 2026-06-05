#!/usr/bin/env bash
#
# dev-local.sh — run the full Mailtivo-Relay stack locally in DEVELOPMENT mode.
#
# This brings up the dev compose stack (docker-compose.yml + docker-compose.dev.yml):
# the Django dev server (autoreload) + Postgres 16 + a Django-Q2 worker, all on
# settings.dev with DJANGO_DEBUG=True. The source tree is mounted into the
# container, so edits reload live.
#
# Because this runs under DEBUG, dev-only conveniences are available — most
# notably passwordless sign-in at /dev-login/ (and the "Dev sign-in" button on
# the login page). For a production-parity run instead, use ./scripts/prod-local.sh.
#
# On first run it bootstraps .env (copies .env.example) and mints the body
# encryption key (RELAY_FERNET_KEY) so message sending works end to end.
#
# Usage:
#   ./scripts/dev-local.sh [command]
#
# Commands:
#   up        (default) bootstrap env, build images, migrate, start the stack
#   down      stop and remove containers (volumes/data preserved)
#   reset     down + delete volumes (DESTROYS the database and stored bodies)
#   restart   restart the running services
#   logs      follow logs from all services
#   ps        show service status
#   shell     open a Django shell inside the web container
#   migrate   run database migrations and exit
#   build     rebuild images without starting
#   help      show this message
#
set -euo pipefail

# Resolve repo root (parent of this script's dir) and run everything from there.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
WEB_PORT_DEFAULT=8000

# The dev overlay: base stack + dev server, source mount, settings.dev.
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.dev.yml)

# ── pretty output ───────────────────────────────────────────────────────────
info()  { printf '\033[36m[dev-local]\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m[dev-local]\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m[dev-local]\033[0m %s\n' "$*"; }
die()   { printf '\033[31m[dev-local]\033[0m %s\n' "$*" >&2; exit 1; }

# ── docker compose detection ──────────────────────────────────────────────────
detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE=(docker compose)
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE=(docker-compose)
    else
        die "Docker Compose not found. Install Docker Desktop / the compose plugin."
    fi
}

require_docker() {
    command -v docker >/dev/null 2>&1 || die "Docker is not installed or not on PATH."
    docker info >/dev/null 2>&1 || die "Docker daemon is not running. Start Docker and retry."
    detect_compose
}

# Run docker compose with the dev overlay files always applied.
compose() { "${COMPOSE[@]}" "${COMPOSE_FILES[@]}" "$@"; }

# ── secret generation (no Python needed) ─────────────────────────────────────
# Fernet key = URL-safe base64 of 32 random bytes (44 chars, trailing '=').
gen_fernet() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr '+/' '-_'
    else
        head -c 32 /dev/urandom | base64 | tr '+/' '-_'
    fi
}

# Set or replace a KEY=value line in .env (appends if absent).
set_env_var() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        # Use a temp file; value may contain '/' so avoid sed delimiters.
        local tmp; tmp="$(mktemp)"
        awk -v k="$key" -v v="$value" \
            'BEGIN{FS=OFS="="} $1==k {print k"="v; next} {print}' \
            "$ENV_FILE" >"$tmp"
        mv "$tmp" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
    fi
}

# Current value of KEY in .env (empty if missing).
get_env_var() {
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true
}

is_placeholder() {
    case "$1" in
        ""|*change-me*|*dev-only*|*dev-insecure*|*replace-me*) return 0 ;;
        *) return 1 ;;
    esac
}

bootstrap_env() {
    if [ ! -f "$ENV_FILE" ]; then
        [ -f "$ENV_EXAMPLE" ] || die "Neither .env nor .env.example found."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        info "Created .env from .env.example"
    fi

    # Body encryption is on in every mode — mint a key so sending works. The
    # Django SECRET_KEY can stay at the dev default; settings.dev never runs in
    # the open, so we don't force-mint it here the way prod-local does.
    local fernet
    fernet="$(get_env_var RELAY_FERNET_KEY)"
    if is_placeholder "$fernet"; then
        set_env_var RELAY_FERNET_KEY "$(gen_fernet)"
        ok "Minted a fresh RELAY_FERNET_KEY (body encryption)"
    fi
}

web_port() {
    local p; p="$(get_env_var WEB_PORT)"
    printf '%s' "${p:-$WEB_PORT_DEFAULT}"
}

# ── wait for the web service to answer ───────────────────────────────────────
# The dev overlay runs `manage runserver`, which has no compose healthcheck, so
# we poll /healthz/ directly instead of relying on a health status.
wait_healthy() {
    local port; port="$(web_port)"
    info "Waiting for the dev server to answer…"
    local tries=0 max=60
    while [ "$tries" -lt "$max" ]; do
        if curl -fsS "http://localhost:${port}/healthz/" >/dev/null 2>&1; then
            ok "Dev server is up."
            return 0
        fi
        tries=$((tries + 1))
        sleep 2
    done
    warn "Dev server did not answer after $((max * 2))s. Check: ./scripts/dev-local.sh logs"
    return 1
}

print_next_steps() {
    local port; port="$(web_port)"
    cat <<EOF

$(ok "Dev stack is up (autoreload on, source mounted, DEBUG=True).")

  Panel / onboarding : http://localhost:${port}/
  Dev sign-in        : http://localhost:${port}/dev-login/   (first admin, no password)
  API base URL       : http://localhost:${port}/api/v1
  Health             : http://localhost:${port}/healthz/

  Logs   : ./scripts/dev-local.sh logs
  Stop   : ./scripts/dev-local.sh down
  Wipe   : ./scripts/dev-local.sh reset   (deletes the database!)

EOF
    info "Outbound email prints to the console backend — watch the logs for magic links."
    info "Run onboarding once to create the first admin, then /dev-login/ signs you in."
}

# ── commands ─────────────────────────────────────────────────────────────────
cmd_up() {
    require_docker
    bootstrap_env
    info "Building images…"
    compose build
    # runserver does not apply migrations the way the gunicorn `web` mode does,
    # so run them as a one-shot first (this also waits for Postgres healthy).
    info "Applying migrations…"
    compose run --rm web migrate
    info "Starting the dev stack…"
    compose up -d
    wait_healthy || true
    print_next_steps
}

cmd_down()    { require_docker; compose down; ok "Stack stopped (data preserved)."; }
cmd_reset()   {
    require_docker
    warn "This deletes Postgres data, stored message bodies, and static volumes."
    compose down -v
    ok "Stack and volumes removed."
}
cmd_restart() { require_docker; compose restart; ok "Services restarted."; }
cmd_logs()    { require_docker; compose logs -f --tail=200; }
cmd_ps()      { require_docker; compose ps; }
cmd_build()   { require_docker; bootstrap_env; compose build; }
cmd_shell()   { require_docker; compose exec web python manage.py shell; }
cmd_migrate() { require_docker; compose run --rm web migrate; }

cmd_help() {
    # Print the header comment block (from line 3 until the first non-# line),
    # stripping the leading "# ".
    awk 'NR>=3 && /^#/ {sub(/^# ?/,""); print; next} NR>=3 {exit}' "${BASH_SOURCE[0]}"
}

main() {
    case "${1:-up}" in
        up)       cmd_up ;;
        down)     cmd_down ;;
        reset)    cmd_reset ;;
        restart)  cmd_restart ;;
        logs)     cmd_logs ;;
        ps|status) cmd_ps ;;
        build)    cmd_build ;;
        shell)    cmd_shell ;;
        migrate)  cmd_migrate ;;
        help|-h|--help) cmd_help ;;
        *) die "Unknown command '$1'. Run './scripts/dev-local.sh help'." ;;
    esac
}

main "$@"
