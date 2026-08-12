#!/usr/bin/env bash
# Lightweight checks for deploy.sh validation helpers.
# Plain bash; no testing framework. Fails nonzero on any failed check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/deploy-validation.sh
source "$REPO_ROOT/scripts/lib/deploy-validation.sh"

failures=0

expect_ok() {
  local db_path="$1"
  local label="$2"
  if is_db_path_persisted "$db_path"; then
    echo "PASS: $label"
  else
    echo "FAIL: $label (expected persisted, got rejected)" >&2
    failures=$((failures + 1))
  fi
}

expect_rejected() {
  local db_path="$1"
  local label="$2"
  if is_db_path_persisted "$db_path"; then
    echo "FAIL: $label (expected rejected, got persisted)" >&2
    failures=$((failures + 1))
  else
    echo "PASS: $label"
  fi
}

expect_ok "/app/instance/remotescout.db" "/app/instance/remotescout.db passes"
expect_ok "/app/instance/data/remotescout.db" "/app/instance/data/remotescout.db passes"
expect_rejected "" "unset path rejected by predicate (deploy.sh only checks when set)"
expect_rejected "/app/remotescout.db" "/app/remotescout.db rejected"
expect_rejected "/app/instance" "/app/instance (no file) rejected"
expect_rejected "/app/instancex/remotescout.db" "/app/instancex/... prefix collision rejected"
expect_rejected "/tmp/remotescout.db" "/tmp/remotescout.db rejected"
expect_rejected "/remotescout.db" "/remotescout.db rejected"
expect_rejected "instance/remotescout.db" "relative path rejected"
expect_rejected "/app/instance/../remotescout.db" "path traversal rejected"

if (( failures > 0 )); then
  echo "$failures validation check(s) failed." >&2
  exit 1
fi

echo "All deploy validation checks passed."
