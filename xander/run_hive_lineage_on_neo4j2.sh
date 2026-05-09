#!/bin/sh
# Run on neo4j2 (SSH session or cron). HMS Thrift ingestion + storage lineage for default.dw_order_v1.
#
# Deploy (flat names for put2 FTP):
#   cp xander/recipes/hive_metastore_dw_order_v1_lineage.yml ./hive_metastore_dw_order_v1_lineage.yml
#   cp xander/run_hive_lineage_on_neo4j2.sh ./run_hive_lineage_on_neo4j2.sh
#   put2 hive_metastore_dw_order_v1_lineage.yml ; put2 run_hive_lineage_on_neo4j2.sh
# On neo4j2 after get2: mv both → /data/datahub/scripts/ && chmod +x run_hive_lineage_on_neo4j2.sh
#
# Bastion-safe remote trigger (no sh /path on host): see xander/exec_hive_lineage_ingest_via_bastion.example.sh
#
# If ingest fails at _sqlglot_patch / DiffApplyError: venv sqlglot drifted vs bundled patches.
#   docker exec CTR /home/datahub/.venv/bin/pip install sqlglot==30.0.3 --force-reinstall --no-cache-dir
# Align /metadata-ingestion/.../_sqlglot_patch.py with OSS DataHub same minor as CLI if image is stale.
#
# Environment (defaults suit internal HMS + compose network):
#   HMS_THRIFT_HOST HMS_THRIFT_PORT DATAHUB_GMS_URL (inside container prefer http://datahub-gms:8080)
#   DATAHUB_GMS_TOKEN optional; DATAHUB_ACTIONS_CONTAINER to pin container name
set -e
SCRIPT_DIR=/data/datahub/scripts
RECIPE_NAME=hive_metastore_dw_order_v1_lineage.yml
HOST_RECIPE="$SCRIPT_DIR/$RECIPE_NAME"
TMP_RECIPE="/tmp/$RECIPE_NAME"

export HMS_THRIFT_HOST="${HMS_THRIFT_HOST:-hiveserver5.dp.data.bj1.wormpex.com}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"

if [ "${RUN_ON_HOST:-0}" = "1" ]; then
  export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
  exec datahub ingest -c "$HOST_RECIPE"
fi

CTR="${DATAHUB_ACTIONS_CONTAINER:-}"
if [ -z "$CTR" ]; then
  for n in $(docker ps --format '{{.Names}}'); do
    case "$n" in *datahub-actions*) CTR=$n; break ;; esac
  done
fi
if [ -z "$CTR" ]; then
  CTR=root-datahub-actions-1
fi

GMS="${DATAHUB_GMS_URL:-http://datahub-gms:8080}"

docker cp "$HOST_RECIPE" "$CTR:$TMP_RECIPE"

exec docker exec \
  -e DATAHUB_TELEMETRY_ENABLED=false \
  -e HMS_THRIFT_HOST="$HMS_THRIFT_HOST" \
  -e HMS_THRIFT_PORT="$HMS_THRIFT_PORT" \
  -e DATAHUB_GMS_URL="$GMS" \
  -e DATAHUB_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-}" \
  "$CTR" datahub ingest -c "$TMP_RECIPE"
