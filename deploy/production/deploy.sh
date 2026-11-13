#!/usr/bin/env bash
# Server-side deploy script. Normally invoked by publish.ps1, but it can also
# be run directly on the server:
#
#   bash deploy.sh /opt/xam /path/to/uploaded/config
#
# It clones/updates the three repositories, installs the production compose
# files, builds images and restarts the stack.

set -euo pipefail

# Keep the project name stable so volumes are named xam_*.
unset COMPOSE_PROJECT_NAME

APP_DIR="${1:-/opt/xam}"
CONFIG_SRC="${2:-/tmp/xam-production-config}"
REPO_BRANCH="${REPO_BRANCH:-main}"
CONFIG_DST="$APP_DIR/xam_call_service/deploy/production"
COMPOSE_FILE="$CONFIG_DST/docker-compose.prod.yml"
ENV_FILE="$CONFIG_DST/.env.production"

cleanup() {
    if [ "$CONFIG_SRC" = "/tmp/xam-production-config" ]; then
        rm -rf "$CONFIG_SRC" /tmp/xam-production-config.tgz
    fi
}
trap cleanup EXIT

log() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required"
command -v docker >/dev/null 2>&1 || die "docker is required"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is required"

ensure_repo() {
    local name="$1"
    local url="$2"
    local dir="$APP_DIR/$name"

    if [ -d "$dir/.git" ]; then
        log "Updating $name"
        git -C "$dir" fetch --prune origin
        git -C "$dir" checkout -q -f -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
    else
        # The first deploy may have been uploaded as a plain directory snapshot.
        # Adopt it into git without deleting untracked files such as
        # deploy/production/.env.production.
        log "Adopting $name into git"
        mkdir -p "$dir"
        git -C "$dir" init -q
        git -C "$dir" remote add origin "$url" 2>/dev/null || \
            git -C "$dir" remote set-url origin "$url"
        git -C "$dir" fetch --prune origin
        git -C "$dir" checkout -q -f -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
    fi
}

mkdir -p "$APP_DIR"
ensure_repo xam_frontend https://github.com/Macson2023/xam_frontend.git
ensure_repo xam_backend https://github.com/Macson2023/xam_backend.git
ensure_repo xam_call_service https://github.com/Macson2023/xam_call_service.git

log "Installing production configuration"
mkdir -p "$CONFIG_DST"
for file in docker-compose.prod.yml Caddyfile deploy.sh; do
    [ -f "$CONFIG_SRC/$file" ] || die "missing $CONFIG_SRC/$file"
    cp "$CONFIG_SRC/$file" "$CONFIG_DST/$file"
done
chmod +x "$CONFIG_DST/deploy.sh"

if [ -f "$CONFIG_SRC/.env.production" ]; then
    cp "$CONFIG_SRC/.env.production" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
elif [ ! -f "$ENV_FILE" ]; then
    die "missing $ENV_FILE; copy .env.production.example, set the secrets, and redeploy"
fi

if grep -Eq '^(SECRET_KEY|POSTGRES_PASSWORD|TURN_CREDENTIAL)=.*CHANGE_ME' "$ENV_FILE"; then
    die "$ENV_FILE still contains CHANGE_ME placeholders; set real secrets first"
fi

if grep -q '^SERVER_IP=203\.0\.113\.10$' "$ENV_FILE" || \
   grep -q '^TURN_EXTERNAL_IP=203\.0\.113\.10$' "$ENV_FILE"; then
    warn "SERVER_IP/TURN_EXTERNAL_IP is still the documentation placeholder 203.0.113.10"
    warn "TURN and maybe WebRTC will not work until the real server IP is set"
fi

COMPOSE=(
    docker compose
    --project-directory "$CONFIG_DST"
    --env-file "$ENV_FILE"
    -f "$COMPOSE_FILE"
)

log "Building images"
"${COMPOSE[@]}" build --pull

log "Starting stack"
"${COMPOSE[@]}" up -d --remove-orphans

wait_for_container() {
    local name="$1"
    local tries="${2:-120}"

    for _ in $(seq 1 "$tries"); do
        local status
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || true)"
        case "$status" in
            healthy|running) return 0 ;;
            unhealthy|exited|dead)
                die "$name is $status; inspect with: ${COMPOSE[*]} logs $name"
                ;;
        esac
        sleep 2
    done

    die "$name did not become healthy in time; inspect with: ${COMPOSE[*]} logs $name"
}

log "Waiting for containers"
wait_for_container xam_db 60
wait_for_container xam_backend 150
wait_for_container xam_calls 90
wait_for_container xam_frontend 60
wait_for_container xam_caddy 30

log "Current status"
"${COMPOSE[@]}" ps

FRONTEND_DOMAIN="$(sed -n 's/^FRONTEND_DOMAIN=//p' "$ENV_FILE" | tail -n 1)"
printf '\nDeployed:\n'
printf '  Frontend:     https://%s\n' "${FRONTEND_DOMAIN:-xamlotoc.ru}"
printf '  Backend:      https://backend.xamlotoc.ru/health\n'
printf '  Call service: https://callservice.xamlotoc.ru/health\n'
