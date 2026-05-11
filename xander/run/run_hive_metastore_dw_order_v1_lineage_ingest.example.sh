#!/usr/bin/env sh
# Run Hive Metastore ingestion with storage lineage for default.dw_order_v1.
# Typical (neo4j2 / actions container host): HMS Thrift Kerberos off.
#
#   export HMS_THRIFT_HOST=hiveserver5.dp.data.bj1.wormpex.com
#   export HMS_THRIFT_PORT=9083
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080   # or http://datahub-gms:8080 inside compose network
#   # optional: export DATAHUB_GMS_TOKEN=...
#   sh xander/run/run_hive_metastore_dw_order_v1_lineage_ingest.example.sh
#
# Post-ingest: xander/run/verify_dw_order_v1_storage_lineage.example.sh
# BLF context (schedule + DDL cues): xander/run/blf_collect_dw_order_v1_facts.example.sh
# Extra table-table edges (optional): xander/notes/openlineage_optional_note.txt
#
# neo4j2 server: deploy flat files → /data/datahub/scripts/, then xander/run/run_hive_lineage_on_neo4j2.sh
# or from laptop only docker: xander/run/exec_hive_lineage_ingest_via_bastion.example.sh
#
# Recipe path is resolved relative to repo root when run from datahub checkout.
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RECIPE="$ROOT/xander/recipes/hive_metastore_dw_order_v1_lineage.yml"

: "${HMS_THRIFT_HOST:?set HMS_THRIFT_HOST}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"
: "${DATAHUB_GMS_URL:?set DATAHUB_GMS_URL}"

exec datahub ingest -c "$RECIPE"
