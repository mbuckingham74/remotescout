#!/usr/bin/env bash
# deploy.sh — Deploy Remote Scout from the local Mac to the forkstech
# production server (rsync + SSH + remote Docker Compose).
#
# Usage (from the Remote Scout repository root):
#
#   ./deploy.sh
#
# Flow:
#   resolve script/repo root (no arguments accepted)
#   -> local validation (clean working tree on main, tools, baseline)
#   -> print deployment target (exact local HEAD being deployed)
#   -> FOXGUARD gate (local, mandatory; before any transfer)
#   -> rsync exact local deployment payload to the server
#   -> remote validation (.env, docker, compose, npm_network, db path)
#   -> remote docker compose build/up
#   -> remote bounded container readiness
#   -> remote image/build-cache pruning
#   -> FINAL remote /healthz check (required; success is not printed before)
#
# This script never runs Git on the server, never touches cron, systemd
# timers, Nginx Proxy Manager, Authelia, DNS, or the SQLite database.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
cd "$REPO_ROOT"

SSH_USER="michael"
SSH_HOST="100.120.233.4"
SSH_TARGET="$SSH_USER@$SSH_HOST"
REMOTE_DIR="${REMOTESCOUT_REMOTE_DIR:-/home/michael/deployments/remotescout}"
STATE_DIR="${REMOTESCOUT_STATE_DIR:-/home/michael/state/remotescout}"
GIT_BRANCH="main"
SERVICE_NAME="remotescout-app"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

if [[ $# -gt 0 ]]; then
  echo "ERROR: deploy.sh takes no arguments." >&2
  exit 1
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Missing required command: $1" >&2
    exit 1
  fi
}

require_command git
require_command ssh
require_command rsync
require_command npx

echo "=== Deploying Remote Scout to forkstech ==="
echo "Repo root:  $REPO_ROOT"
echo "Server:     $SSH_TARGET"
echo "Remote dir: $REMOTE_DIR"

# --- local repository state ---
if [[ ! -d "$REPO_ROOT/.git" ]]; then
  echo "ERROR: $REPO_ROOT is not a git working tree." >&2
  exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "$GIT_BRANCH" ]]; then
  echo "ERROR: Deploys must run from branch $GIT_BRANCH (current: $current_branch)." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "ERROR: Git working tree is dirty. Commit or stash changes before deploy." >&2
  exit 1
fi

# --- exact deployment target (local clean HEAD; no git operations on the server) ---
TARGET_COMMIT="$(git rev-parse HEAD)"
TARGET_SHORT="$(git rev-parse --short "$TARGET_COMMIT")"
TARGET_SUBJECT="$(git log -1 --pretty=%s "$TARGET_COMMIT")"
echo "Deployment target: $TARGET_SHORT $TARGET_SUBJECT"

# --- Foxguard gate (local, mandatory; no bypass) ---
if [[ ! -f "$REPO_ROOT/foxguard-baseline.json" ]]; then
  echo "ERROR: Missing Foxguard baseline file: $REPO_ROOT/foxguard-baseline.json" >&2
  echo "Run from this repo root: foxguard --write-baseline foxguard-baseline.json ." >&2
  exit 1
fi

echo "Running Foxguard security scan..."
if ! npx foxguard --baseline foxguard-baseline.json .; then
  echo "Foxguard gate FAILED. Deployment aborted before any transfer." >&2
  exit 1
fi
echo "Foxguard gate PASSED for $TARGET_SHORT"

# --- transfer the exact local deployment payload ---
echo "Ensuring remote directory exists..."
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "mkdir -p '$REMOTE_DIR'"

echo "Syncing deployment payload to $SSH_TARGET:$REMOTE_DIR ..."
rsync -avz --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude .git \
  --exclude .env \
  --exclude .deployment.json \
  --exclude .venv \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  --exclude .pytest_cache \
  --exclude instance \
  --exclude '*.db' \
  --exclude '*.db-shm' \
  --exclude '*.db-wal' \
  --exclude .DS_Store \
  --exclude deploy.sh \
  --exclude tests \
  --exclude scripts/smoke_recommendations.py \
  --exclude scripts/test-deploy-validation.sh \
  "$REPO_ROOT/" "$SSH_TARGET:$REMOTE_DIR/"

# --- remote deployment (single SSH session; server state lives on the server) ---
echo "Running remote deployment..."
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "export REMOTESCOUT_REMOTE_DIR='$REMOTE_DIR' REMOTESCOUT_STATE_DIR='$STATE_DIR'; bash -s" <<'REMOTE_EOF'
set -euo pipefail

SERVICE_NAME="remotescout-app"
REMOTE_DIR="${REMOTESCOUT_REMOTE_DIR:-/home/michael/deployments/remotescout}"
STATE_DIR="${REMOTESCOUT_STATE_DIR:-/home/michael/state/remotescout}"
HEALTH_URL="http://127.0.0.1:8000/healthz"
STARTUP_TIMEOUT_SECONDS=120
STARTUP_POLL_SECONDS=5
FINAL_HEALTH_ATTEMPTS=6
FINAL_HEALTH_INTERVAL_SECONDS=5

cd "$REMOTE_DIR"

if [[ ! -f "$REMOTE_DIR/.env" ]]; then
  echo "ERROR: Missing $REMOTE_DIR/.env on the server." >&2
  echo "The production .env is server-owned and is never transferred." >&2
  exit 1
fi

api_key="$(sed -n 's/^ANTHROPIC_API_KEY=//p' "$REMOTE_DIR/.env" | head -n1 | tr -d '\r')"
if [[ -z "$api_key" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is missing or empty in $REMOTE_DIR/.env" >&2
  exit 1
fi

source "$REMOTE_DIR/scripts/lib/deploy-validation.sh"
db_path="$(sed -n 's/^REMOTESCOUT_DATABASE_PATH=//p' "$REMOTE_DIR/.env" | head -n1 | tr -d '\r')"
if [[ -n "$db_path" ]] && ! is_db_path_persisted "$db_path"; then
  echo "ERROR: REMOTESCOUT_DATABASE_PATH in $REMOTE_DIR/.env is not persisted by the deployed volume: $db_path" >&2
  echo "The container mounts /home/michael/state/remotescout at /app/instance; the database must live under /app/instance/." >&2
  echo "Unset the variable (container default: /app/instance/remotescout.db) or set a valid path." >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker is not available on the server." >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "ERROR: docker compose is not available on the server." >&2
  exit 1
}
docker network inspect npm_network >/dev/null 2>&1 || {
  echo "ERROR: Docker network 'npm_network' does not exist on the server." >&2
  echo "It is created by the host's Nginx Proxy Manager stack." >&2
  exit 1
}

mkdir -p "$STATE_DIR"

echo "Building updated image..."
docker compose build --pull "$SERVICE_NAME"

echo "Starting updated container..."
docker compose up -d --no-deps "$SERVICE_NAME"

container_id="$(docker compose ps -q "$SERVICE_NAME")"
if [[ -z "$container_id" ]]; then
  echo "ERROR: Could not determine container ID for $SERVICE_NAME." >&2
  exit 1
fi

echo "Waiting for $SERVICE_NAME to become healthy..."
deadline=$((SECONDS + STARTUP_TIMEOUT_SECONDS))
while true; do
  health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"

  case "$health_status" in
    healthy)
      echo "$SERVICE_NAME is healthy."
      break
      ;;
    starting|running|created|restarting)
      if (( SECONDS >= deadline )); then
        echo "ERROR: Timed out after ${STARTUP_TIMEOUT_SECONDS}s waiting for $SERVICE_NAME to become healthy." >&2
        docker compose logs --tail=100 "$SERVICE_NAME" >&2 || true
        exit 1
      fi
      sleep "$STARTUP_POLL_SECONDS"
      ;;
    *)
      echo "ERROR: $SERVICE_NAME reported health status: $health_status" >&2
      docker compose logs --tail=100 "$SERVICE_NAME" >&2 || true
      exit 1
      ;;
  esac
done

docker image prune -f >/dev/null 2>&1 || true
docker builder prune -af --filter 'until=24h' >/dev/null 2>&1 || true

echo "Running final /healthz check..."
health_ok=0
for attempt in $(seq 1 "$FINAL_HEALTH_ATTEMPTS"); do
  if docker exec "$container_id" python -c "import urllib.request; urllib.request.urlopen('$HEALTH_URL', timeout=10)" >/dev/null 2>&1; then
    echo "Health check passed: $HEALTH_URL"
    health_ok=1
    break
  fi
  echo "Health attempt $attempt/$FINAL_HEALTH_ATTEMPTS failed; retrying in ${FINAL_HEALTH_INTERVAL_SECONDS}s..."
  sleep "$FINAL_HEALTH_INTERVAL_SECONDS"
done

if [[ "$health_ok" -ne 1 ]]; then
  echo "ERROR: /healthz did not return 200 after deploy." >&2
  docker compose logs --tail=100 "$SERVICE_NAME" >&2 || true
  exit 1
fi
REMOTE_EOF

echo ""
echo "=== Deployment complete ==="
echo "Service: $SERVICE_NAME"
echo "Commit:  $TARGET_SHORT $TARGET_SUBJECT — scanned by Foxguard locally and deployed"
echo "Health:  remote /healthz returned 200"
