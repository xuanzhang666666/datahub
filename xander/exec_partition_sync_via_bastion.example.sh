#!/bin/sh
# One-off from your laptop: bastion → neo4j2, only allowlisted remote commands (docker cp / docker exec).
# Pair with: xander/sync_partition_stats_to_datahub_trino.py deployed to neo4j2 /data/datahub/scripts/
# (put2 + get2 or rsync). For cron or shell on neo4j2 itself, use xander/run_partition_stats_on_neo4j2.sh instead.
# Adjust BASTION_KEY / REMOTE / CTR if your compose container name is not root-datahub-actions-1.

set -e
BASTION_KEY="${BASTION_KEY:-$HOME/.ssh/agent-bastion}"
CTR="${DATAHUB_ACTIONS_CONTAINER:-root-datahub-actions-1}"
REMOTE="@neo4j2.dp.data.bj1"
BASE="ssh -i $BASTION_KEY -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11"

"$BASE" "$REMOTE docker cp /data/datahub/scripts/sync_partition_stats_to_datahub_trino.py ${CTR}:/tmp/sync_partition_stats_to_datahub_trino.py"
"$BASE" "$REMOTE docker exec -e DATAHUB_GMS_URL=http://datahub-gms:8080 -e TRINO_HOST=${TRINO_HOST:-10.253.7.167} -e TRINO_PORT=${TRINO_PORT:-8081} -e TRINO_USER=${TRINO_USER:-xuan.zhang} ${CTR} python3 /tmp/sync_partition_stats_to_datahub_trino.py $*"
