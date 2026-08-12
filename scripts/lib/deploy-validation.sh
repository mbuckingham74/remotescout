#!/usr/bin/env bash
# Deployment validation helpers shared by deploy.sh.
# Sourced, not executed; contains no top-level side effects.

# is_db_path_persisted DB_PATH
# Returns 0 only when DB_PATH lies inside /app/instance/, which is the
# volume mount that persists the production SQLite database across deploys.
# Paths containing ".." are rejected because they can escape the volume.
is_db_path_persisted() {
  local db_path="$1"
  [[ -n "$db_path" && "$db_path" == /app/instance/* && "$db_path" != *..* ]]
}
