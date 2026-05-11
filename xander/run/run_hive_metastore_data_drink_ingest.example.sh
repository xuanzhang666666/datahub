#!/usr/bin/env sh
# Run Hive Metastore ingestion for all tables in data_drink.
#
#   export HMS_THRIFT_HOST=hiveserver5.dp.data.bj1.wormpex.com
#   export HMS_THRIFT_PORT=9083
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   # optional: export DATAHUB_GMS_TOKEN=...
#   sh xander/run/run_hive_metastore_data_drink_ingest.example.sh
#
# neo4j2: flat FTP names → put2 hive_metastore_data_drink.yml ; put2 run_hive_data_drink_on_neo4j2.sh
#         then on host: get2 → /data/datahub/scripts/
#
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RECIPE="$ROOT/xander/recipes/hive_metastore_data_drink.yml"

: "${HMS_THRIFT_HOST:?set HMS_THRIFT_HOST}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"
: "${DATAHUB_GMS_URL:?set DATAHUB_GMS_URL}"

exec datahub ingest -c "$RECIPE"
