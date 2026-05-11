#!/usr/bin/env sh
# Ingest default + data_drink + 14 data_* Hive DBs in one pipeline (see recipe header).
#
#   export HMS_THRIFT_HOST=hiveserver5.dp.data.bj1.wormpex.com
#   export HMS_THRIFT_PORT=9083
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   sh xander/run/run_hive_metastore_blf_prod_core_dbs_ingest.example.sh
#
# neo4j2: put2 hive_metastore_blf_prod_core_dbs.yml ; put2 run_hive_blf_prod_core_dbs_on_neo4j2.sh
#
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RECIPE="$ROOT/xander/recipes/hive_metastore_blf_prod_core_dbs.yml"

: "${HMS_THRIFT_HOST:?set HMS_THRIFT_HOST}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"
: "${DATAHUB_GMS_URL:?set DATAHUB_GMS_URL}"

exec datahub ingest -c "$RECIPE"
