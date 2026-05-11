#!/bin/sh
# neo4j2: HMS Thrift ingest for hive_metastore_blf_prod_core_dbs.yml (default + data_drink + 14 data_*).
# Recipe pins env=PROD, platform_instance=blf-prod-hive, catalog_name=hive — keep in sync with xander/recipes copy.
#
# Deploy (flat FTP names):
#   put2 hive_metastore_blf_prod_core_dbs.yml
#   put2 run_hive_blf_prod_core_dbs_on_neo4j2.sh
# On host: get2 → mv to /data/datahub/scripts/ ; chmod +x run_hive_blf_prod_core_dbs_on_neo4j2.sh
#
# Bastion: prefer docker cp + docker exec (see prior data_drink / multi_dbs runs).
#
set -e
SCRIPT_DIR=/data/datahub/scripts
RECIPE_NAME=hive_metastore_blf_prod_core_dbs.yml
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
