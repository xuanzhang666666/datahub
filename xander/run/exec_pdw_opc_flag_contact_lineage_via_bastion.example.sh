#!/bin/sh
# Laptop → bastion → neo4j2: ingest HMS lineage for default.pdw_opc_flag_contact only.
#
# Put recipe on neo4j2 host first (e.g. /data/datahub/scripts/hive_metastore_pdw_opc_flag_contact_lineage.yml).

set -e
BASTION_KEY="${BASTION_KEY:-$HOME/.ssh/agent-bastion}"
CTR="${DATAHUB_ACTIONS_CONTAINER:-root-datahub-actions-1}"
REMOTE="@neo4j2.dp.data.bj1"
BASE="ssh -i $BASTION_KEY -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11"

HMS="${HMS_THRIFT_HOST:-hiveserver5.dp.data.bj1.wormpex.com}"
HP="${HMS_THRIFT_PORT:-9083}"
RECIPE="${HIVE_LINEAGE_RECIPE:-hive_metastore_pdw_opc_flag_contact_lineage.yml}"

"$BASE" "$REMOTE docker cp /data/datahub/scripts/$RECIPE ${CTR}:/tmp/$RECIPE"
"$BASE" "$REMOTE docker exec -e DATAHUB_TELEMETRY_ENABLED=false -e HMS_THRIFT_HOST=$HMS -e HMS_THRIFT_PORT=$HP -e DATAHUB_GMS_URL=http://datahub-gms:8080 ${CTR} datahub ingest -c /tmp/$RECIPE"
