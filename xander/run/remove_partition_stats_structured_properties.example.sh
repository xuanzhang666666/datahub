#!/bin/sh
# Bastion → neo4j2: run remove script inside the actions container (same pattern as exec_partition_sync_via_bastion.example.sh).
# Deploy the Python file to the server first, e.g. /data/datahub/scripts/remove_partition_stats_structured_properties.py
# (put2/get2 or rsync from your laptop).

set -e
BASTION_KEY="${BASTION_KEY:-$HOME/.ssh/agent-bastion}"
CTR="${DATAHUB_ACTIONS_CONTAINER:-root-datahub-actions-1}"
REMOTE="@neo4j2.dp.data.bj1"
BASE="ssh -i $BASTION_KEY -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11"

"$BASE" "$REMOTE docker cp /data/datahub/scripts/remove_partition_stats_structured_properties.py ${CTR}:/tmp/remove_partition_stats_structured_properties.py"
"$BASE" "$REMOTE docker exec -e DATAHUB_GMS_URL=http://datahub-gms:8080 ${CTR} python3 /tmp/remove_partition_stats_structured_properties.py $*"
