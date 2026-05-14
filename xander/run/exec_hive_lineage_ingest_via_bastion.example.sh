#!/bin/sh
# Laptop → bastion → neo4j2：在从机上执行「单库 HMS 入仓」（与 ingest_hive_database_to_datahub.sh 一致）。
#
# 前置：neo4j2 已部署 /data/datahub/recipes/hive_ingest_one_database.yml 与
#   /data/datahub/scripts/ingest_hive_database_to_datahub.sh（chmod +x）。
#
# 示例库名 default；按需改为 data_logistics 等。勿在一条远程命令里使用 &&（bastion 可能拦截）。

set -e
BASTION_KEY="${BASTION_KEY:-$HOME/.ssh/agent-bastion}"
REMOTE="@neo4j2.dp.data.bj1"
BASE="ssh -i $BASTION_KEY -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11"

"$BASE" "$REMOTE /data/datahub/scripts/ingest_hive_database_to_datahub.sh default"
