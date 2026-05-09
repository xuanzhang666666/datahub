#!/bin/sh
# Example: run FROM YOUR LAPTOP to trigger sync on neo4j2 (two SSH hops; no && in one bastion command).
# Adjust BASTION_KEY / REMOTE_HOST / CTR if your compose prefix is not root-.

set -e
BASTION_KEY="${BASTION_KEY:-$HOME/.ssh/agent-bastion}"
CTR="${DATAHUB_ACTIONS_CONTAINER:-root-datahub-actions-1}"
REMOTE="@neo4j2.dp.data.bj1"
BASE="ssh -i $BASTION_KEY -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11"

"$BASE" "$REMOTE docker cp /data/datahub/scripts/sync_partition_stats_to_datahub_trino.py ${CTR}:/tmp/sync_partition_stats_to_datahub_trino.py"
"$BASE" "$REMOTE docker exec -e DATAHUB_GMS_URL=http://datahub-gms:8080 -e TRINO_HOST=${TRINO_HOST:-10.253.7.167} -e TRINO_PORT=${TRINO_PORT:-8081} -e TRINO_USER=${TRINO_USER:-xuan.zhang} ${CTR} python3 /tmp/sync_partition_stats_to_datahub_trino.py $*"
