#!/bin/sh
# Run on the neo4j2 host (SSH session or cron). Deploy xander/sync_partition_stats_to_datahub_trino.py
# beside this script under SCRIPT_DIR (/data/datahub/scripts/ after put2/get2 + mv).
#
# Default path: docker cp into datahub-actions, then docker exec python3 (matches agent-bastion allowlist).
# Laptop → bastion remote trigger without logging into neo4j2: use xander/exec_partition_sync_via_bastion.example.sh.
#
# Optional: RUN_ON_HOST=1 uses host python3 + DATAHUB_GMS_URL=http://127.0.0.1:8080 (neo4j2 only, not via bastion chain).
#
# Trino: no password by default; set TRINO_PASSWORD only if your coordinator uses HTTP basic auth.
set -e
SCRIPT_DIR=/data/datahub/scripts
PY=sync_partition_stats_to_datahub_trino.py
HOST_PY="$SCRIPT_DIR/$PY"
TMP_IN_CT="/tmp/$PY"

export TRINO_HOST="${TRINO_HOST:-10.253.7.167}"
export TRINO_PORT="${TRINO_PORT:-8081}"
export TRINO_USER="${TRINO_USER:-xuan.zhang}"
export TRINO_CATALOG="${TRINO_CATALOG:-hive}"
export TRINO_SCHEMA="${TRINO_SCHEMA:-default}"

if [ "${RUN_ON_HOST:-0}" = "1" ]; then
  cd "$SCRIPT_DIR"
  export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
  exec python3 "$PY" "$@"
fi

# --- docker path (default) ---
CTR="${DATAHUB_ACTIONS_CONTAINER:-}"
if [ -z "$CTR" ]; then
  for n in $(docker ps --format '{{.Names}}'); do
    case "$n" in *datahub-actions*) CTR=$n; break ;; esac
  done
fi
if [ -z "$CTR" ]; then
  CTR=root-datahub-actions-1
fi

docker cp "$HOST_PY" "$CTR:$TMP_IN_CT"

GMS="${DATAHUB_GMS_URL:-http://datahub-gms:8080}"

exec docker exec \
  -e DATAHUB_GMS_URL="$GMS" \
  -e TRINO_HOST="$TRINO_HOST" \
  -e TRINO_PORT="$TRINO_PORT" \
  -e TRINO_USER="$TRINO_USER" \
  -e TRINO_CATALOG="$TRINO_CATALOG" \
  -e TRINO_SCHEMA="$TRINO_SCHEMA" \
  -e TRINO_PASSWORD="${TRINO_PASSWORD:-}" \
  -e DATASET_URN="${DATASET_URN:-}" \
  -e PARTITION_COL="${PARTITION_COL:-}" \
  -e PARTITION_LIMIT="${PARTITION_LIMIT:-}" \
  -e HIVE_TABLE="${HIVE_TABLE:-}" \
  "$CTR" python3 "$TMP_IN_CT" "$@"
