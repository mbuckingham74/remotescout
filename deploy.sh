#!/usr/bin/env bash
# deploy.sh — Production deployment for Remote Scout.
#
# Intended to be invoked manually on the Remote Scout host from any
# working directory:
#
#   ./deploy.sh
#
# Flow:
#   resolve script/repo root (no arguments accepted)
#   -> validate prerequisites / environment / repo state (clean working tree)
#   -> git fetch + capture the exact target commit (origin/main)
#   -> print target short SHA + subject
#   -> Foxguard security gate scans that exact target commit (mandatory,
#      no bypass; staged in a temporary directory, never the live tree)
#   -> fast-forward the production checkout to the already-validated commit
#   -> build and start the application container
#   -> bounded startup readiness
#   -> Docker image/build-cache pruning
#   -> final /healthz check (required; last substantive gate; success is
#      not printed before this)
#
# This script never touches cron, systemd timers, Nginx Proxy Manager,
# DNS, Authelia, or the SQLite database contents.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
cd "$REPO_ROOT"

SERVICE_NAME="remotescout-app"
GIT_REMOTE="origin"
GIT_BRANCH="main"
ENV_FILE="$REPO_ROOT/.env"
CONTAINER_HEALTH_URL="http://127.0.0.1:8000/healthz"
STARTUP_TIMEOUT_SECONDS="${REMOTESCOUT_STARTUP_TIMEOUT_SECONDS:-120}"
STARTUP_POLL_SECONDS="${REMOTESCOUT_STARTUP_POLL_SECONDS:-5}"
FINAL_HEALTH_ATTEMPTS="${REMOTESCOUT_FINAL_HEALTH_ATTEMPTS:-6}"
FINAL_HEALTH_INTERVAL_SECONDS="${REMOTESCOUT_FINAL_HEALTH_INTERVAL_SECONDS:-5}"

# shellcheck source=lib/deploy-validation.sh
source "$REPO_ROOT/scripts/lib/deploy-validation.sh"

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
require_command docker
require_command npx

echo "=== Deploying Remote Scout ==="
echo "Repo root: $REPO_ROOT"

# --- validate environment / inspect state ---
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: Missing production environment file: $ENV_FILE" >&2
  echo "Create it on this host with at least: ANTHROPIC_API_KEY=<key>" >&2
  exit 1
fi

api_key="$(sed -n 's/^ANTHROPIC_API_KEY=//p' "$ENV_FILE" | head -n1 | tr -d '\r')"
if [[ -z "$api_key" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is missing or empty in $ENV_FILE." >&2
  echo "Remote Scout cannot score recommendations without it." >&2
  exit 1
fi

db_path="$(sed -n 's/^REMOTESCOUT_DATABASE_PATH=//p' "$ENV_FILE" | head -n1 | tr -d '\r')"
if [[ -n "$db_path" ]] && ! is_db_path_persisted "$db_path"; then
  echo "ERROR: REMOTESCOUT_DATABASE_PATH in $ENV_FILE is not persisted by the deployed volume: $db_path" >&2
  echo "The container mounts ./instance at /app/instance; the database must live under /app/instance/." >&2
  echo "Valid example: /app/instance/remotescout.db" >&2
  echo "Invalid examples: /app/remotescout.db, /tmp/remotescout.db" >&2
  echo "Unset the variable (container default: /app/instance/remotescout.db) or set a valid path." >&2
  echo "The SQLite database is preserved via the ./instance volume and must never be recreated." >&2
  exit 1
fi

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

if ! docker network inspect npm_network >/dev/null 2>&1; then
  echo "ERROR: Docker network 'npm_network' does not exist." >&2
  echo "It is created by the host's Nginx Proxy Manager stack." >&2
  exit 1
fi

# --- capture the exact target commit (fetch does not touch the working tree) ---
echo "Fetching $GIT_REMOTE/$GIT_BRANCH..."
git fetch "$GIT_REMOTE" "$GIT_BRANCH"

TARGET_COMMIT="$(git rev-parse "$GIT_REMOTE/$GIT_BRANCH")"
TARGET_SHORT="$(git rev-parse --short "$TARGET_COMMIT")"
TARGET_SUBJECT="$(git log -1 --pretty=%s "$TARGET_COMMIT")"
echo "Deployment target: $TARGET_SHORT $TARGET_SUBJECT"

if ! git cat-file -e "$TARGET_COMMIT:foxguard-baseline.json"; then
  echo "ERROR: Target commit $TARGET_COMMIT does not contain foxguard-baseline.json." >&2
  echo "Commit the baseline file to $GIT_BRANCH before deploying." >&2
  exit 1
fi

# --- stage the exact target revision for Foxguard (never scan the live tree) ---
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
git archive "$TARGET_COMMIT" | tar -x -C "$TMP_DIR"

# --- Foxguard gate (mandatory; no bypass) ---
echo "Running Foxguard security scan against $TARGET_COMMIT..."
if ! (cd "$TMP_DIR" && npx foxguard --baseline foxguard-baseline.json .); then
  echo "Foxguard gate FAILED for $TARGET_COMMIT. Deployment aborted before any changes; production checkout unchanged." >&2
  exit 1
fi
echo "Foxguard gate PASSED for $TARGET_COMMIT"

# --- advance the production checkout to the validated commit (ff-only) ---
echo "Advancing production checkout to $TARGET_COMMIT (fast-forward only)..."
git merge --ff-only "$TARGET_COMMIT"

if [[ "$(git rev-parse HEAD)" != "$TARGET_COMMIT" ]]; then
  echo "ERROR: Production checkout is not at the validated commit $TARGET_COMMIT." >&2
  exit 1
fi

# --- deployment mutations ---
echo "Building updated image..."
docker compose build --pull "$SERVICE_NAME"

echo "Starting updated container..."
docker compose up -d --no-deps "$SERVICE_NAME"

# --- bounded startup readiness ---
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

# --- cleanup (host convention; before the final health gate) ---
docker image prune -f >/dev/null 2>&1 || true
docker builder prune -af --filter 'until=24h' >/dev/null 2>&1 || true

# --- final /healthz check (required; last substantive gate) ---
echo "Running final /healthz check..."
last_status=0
for attempt in $(seq 1 "$FINAL_HEALTH_ATTEMPTS"); do
  if docker exec "$container_id" python -c "import urllib.request; urllib.request.urlopen('$CONTAINER_HEALTH_URL', timeout=10)" >/dev/null 2>&1; then
    echo "Health check passed: $CONTAINER_HEALTH_URL"
    last_status=0
    break
  fi
  echo "Health check attempt $attempt/$FINAL_HEALTH_ATTEMPTS failed; retrying in ${FINAL_HEALTH_INTERVAL_SECONDS}s..."
  sleep "$FINAL_HEALTH_INTERVAL_SECONDS"
  last_status=1
done

if [[ "$last_status" -ne 0 ]]; then
  echo "ERROR: /healthz did not return 200 after deploy." >&2
  docker compose logs --tail=100 "$SERVICE_NAME" >&2 || true
  exit 1
fi

echo ""
echo "=== Deployment complete ==="
echo "Service: $SERVICE_NAME"
echo "Commit:  $TARGET_SHORT $TARGET_SUBJECT — scanned by Foxguard and deployed"
echo "Health:  $CONTAINER_HEALTH_URL returned 200"
docker compose ps || true
