#!/usr/bin/env bash
#
# prod-local.sh — run the full Mailtivo-Relay stack locally, the production way.
#
# This brings up the *production* compose stack (docker-compose.yml): gunicorn +
# Postgres 16 + a Django-Q2 worker, all on settings.prod. It is NOT the dev
# overlay — there is no source mount and no auto-reload, so it behaves exactly
# like a deployed instance.
#
# On first run it bootstraps .env (copies .env.example) and mints the two
# secrets prod refuses to run safely without: DJANGO_SECRET_KEY and
# RELAY_FERNET_KEY.
#
# Usage:
#   ./scripts/prod-local.sh [command]
#
# Commands:
#   up        (default) bootstrap env, build images, start the stack, wait healthy
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

# ── pretty output ───────────────────────────────────────────────────────────
info()  { printf '\033[36m[prod-local]\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m[prod-local]\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m[prod-local]\033[0m %s\n' "$*"; }
die()   { printf '\033[31m[prod-local]\033[0m %s\n' "$*" >&2; exit 1; }

# ── docker compose detection ──────────────────────────────────────────────────
detect_compose() {
    # Prod-parity, plus the prod-local overlay that publishes the web port for
    # localhost access (the base compose only exposes it, for hosted proxies).
    local files=(-f docker-compose.yml -f docker-compose.prod-local.yml)
    if docker compose version >/dev/null 2>&1; then
        COMPOSE=(docker compose "${files[@]}")
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE=(docker-compose "${files[@]}")
    else
        die "Docker Compose not found. Install Docker Desktop / the compose plugin."
    fi
}

require_docker() {
    command -v docker >/dev/null 2>&1 || die "Docker is not installed or not on PATH."
    docker info >/dev/null 2>&1 || die "Docker daemon is not running. Start Docker and retry."
    detect_compose
}

# ── secret generation (no Python needed) ─────────────────────────────────────
# Fernet key = URL-safe base64 of 32 random bytes (44 chars, trailing '=').
gen_fernet() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr '+/' '-_'
    else
        head -c 32 /dev/urandom | base64 | tr '+/' '-_'
    fi
}

# Django secret key = any long, random, opaque string.
gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_'
    else
        head -c 48 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'
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

    local secret fernet
    secret="$(get_env_var DJANGO_SECRET_KEY)"
    if is_placeholder "$secret"; then
        set_env_var DJANGO_SECRET_KEY "$(gen_secret)"
        ok "Minted a fresh DJANGO_SECRET_KEY"
    fi

    fernet="$(get_env_var RELAY_FERNET_KEY)"
    if is_placeholder "$fernet"; then
        set_env_var RELAY_FERNET_KEY "$(gen_fernet)"
        ok "Minted a fresh RELAY_FERNET_KEY (body encryption)"
    fi

    # Local prod-over-HTTP: prod.py defaults SSL redirect on. Make sure the
    # stack stays reachable on plain http://localhost without an HTTPS proxy.
    if is_placeholder "$(get_env_var SECURE_SSL_REDIRECT)"; then
        set_env_var SECURE_SSL_REDIRECT "false"
    fi
    if is_placeholder "$(get_env_var SECURE_HSTS_SECONDS)"; then
        set_env_var SECURE_HSTS_SECONDS "0"
    fi
}

web_port() {
    local p; p="$(get_env_var WEB_PORT)"
    printf '%s' "${p:-$WEB_PORT_DEFAULT}"
}

# ── wait for the web healthcheck to go healthy ───────────────────────────────
wait_healthy() {
    local port; port="$(web_port)"
    info "Waiting for the web service to become healthy…"
    local tries=0 max=60
    while [ "$tries" -lt "$max" ]; do
        if curl -fsS "http://localhost:${port}/healthz/" >/dev/null 2>&1; then
            ok "Web is healthy."
            return 0
        fi
        tries=$((tries + 1))
        sleep 2
    done
    warn "Web did not report healthy after $((max * 2))s. Check: ${COMPOSE[*]} logs web"
    return 1
}

print_next_steps() {
    local port; port="$(web_port)"
    cat <<EOF

$(ok "Stack is up — running exactly like production (gunicorn + Postgres + Q2 worker).")

  Panel / onboarding : http://localhost:${port}/
  API base URL       : http://localhost:${port}/api/v1
  Health             : http://localhost:${port}/healthz/

  Logs   : ./scripts/prod-local.sh logs
  Stop   : ./scripts/prod-local.sh down
  Wipe   : ./scripts/prod-local.sh reset   (deletes the database!)

EOF
    warn "Heads-up: settings.prod sets SESSION_COOKIE_SECURE / CSRF_COOKIE_SECURE = True."
    warn "The API works fine over http, but browser panel LOGIN needs HTTPS to keep a"
    warn "session. Put a TLS proxy in front, or use the dev overlay for browser work:"
    warn "  docker compose -f docker-compose.yml -f docker-compose.dev.yml up"
}

# ── commands ─────────────────────────────────────────────────────────────────
cmd_up() {
    require_docker
    bootstrap_env
    info "Building images…"
    "${COMPOSE[@]}" up -d --build
    wait_healthy || true
    print_next_steps
}

cmd_down()    { require_docker; "${COMPOSE[@]}" down; ok "Stack stopped (data preserved)."; }
cmd_reset()   {
    require_docker
    warn "This deletes Postgres data, stored message bodies, and static volumes."
    "${COMPOSE[@]}" down -v
    ok "Stack and volumes removed."
}
cmd_restart() { require_docker; "${COMPOSE[@]}" restart; ok "Services restarted."; }
cmd_logs()    { require_docker; "${COMPOSE[@]}" logs -f --tail=200; }
cmd_ps()      { require_docker; "${COMPOSE[@]}" ps; }
cmd_build()   { require_docker; bootstrap_env; "${COMPOSE[@]}" build; }
cmd_shell()   { require_docker; "${COMPOSE[@]}" exec web python manage.py shell; }
cmd_migrate() { require_docker; "${COMPOSE[@]}" run --rm web migrate; }

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
        *) die "Unknown command '$1'. Run './scripts/prod-local.sh help'." ;;
    esac
}

main "$@"
