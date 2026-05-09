#!/bin/sh
# From laptop through agent-bastion: copy recipe into datahub-actions, run datahub ingest (allowlisted docker only).
#
# Preconditions on neo4j2: hive_metastore_dw_order_v1_lineage.yml already at /data/datahub/scripts/
#   (put2/get2 flat file + mv; same as partition-stats deploy).
#
# Adjust BASTION_KEY / REMOTE / CTR if needed.

set -e
BASTION_KEY="${BASTION_KEY:-$HOME/.ssh/agent-bastion}"
CTR="${DATAHUB_ACTIONS_CONTAINER:-root-datahub-actions-1}"
REMOTE="@neo4j2.dp.data.bj1"
BASE="ssh -i $BASTION_KEY -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11"

HMS="${HMS_THRIFT_HOST:-hiveserver5.dp.data.bj1.wormpex.com}"
HP="${HMS_THRIFT_PORT:-9083}"

"$BASE" "$REMOTE docker cp /data/datahub/scripts/hive_metastore_dw_order_v1_lineage.yml ${CTR}:/tmp/hive_metastore_dw_order_v1_lineage.yml"
"$BASE" "$REMOTE docker exec -e DATAHUB_TELEMETRY_ENABLED=false -e HMS_THRIFT_HOST=$HMS -e HMS_THRIFT_PORT=$HP -e DATAHUB_GMS_URL=http://datahub-gms:8080 ${CTR} datahub ingest -c /tmp/hive_metastore_dw_order_v1_lineage.yml"
