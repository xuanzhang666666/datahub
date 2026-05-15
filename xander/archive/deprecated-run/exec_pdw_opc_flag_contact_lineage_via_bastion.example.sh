#!/bin/sh
# Laptop → bastion → neo4j2：对 default 库做 HMS 入仓（含 default.pdw_opc_flag_contact 等该库下全部表，recipe 已过滤 tmp_/bak_tmp 等）。
#
# 前置：同 exec_hive_lineage_ingest_via_bastion.example.sh（hive_ingest_one_database.yml + ingest 脚本）。

set -e
BASTION_KEY="${BASTION_KEY:-$HOME/.ssh/agent-bastion}"
REMOTE="@neo4j2.dp.data.bj1"
BASE="ssh -i $BASTION_KEY -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11"

"$BASE" "$REMOTE /data/datahub/scripts/ingest_hive_database_to_datahub.sh default"
