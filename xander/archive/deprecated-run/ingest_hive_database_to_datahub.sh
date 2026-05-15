#!/usr/bin/env bash
# 将指定 Hive 库下的表全部导入 DataHub（单脚本 + 单 recipe）。
# 始终在宿主机执行：$PYTHON -m datahub ingest（不经 Docker / datahub-actions 容器）。
#
# 用法:
#   ./ingest_hive_database_to_datahub.sh <库名>
#   HIVE_INGEST_DATABASE=data_logistics ./ingest_hive_database_to_datahub.sh
#
# 部署到 neo4j2:
#   cp xander/recipes/hive_ingest_one_database.yml /data/datahub/recipes/
#   cp xander/run/ingest_hive_database_to_datahub.sh /data/datahub/scripts/
#   chmod +x /data/datahub/scripts/ingest_hive_database_to_datahub.sh
#
# Jenkins Execute shell 示例:
#   export LINEAGE_PYTHON=/opt/anaconda3/bin/python
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   sh /data/datahub/scripts/ingest_hive_database_to_datahub.sh data_logistics
#
# 环境变量（可选）:
#   HMS_THRIFT_HOST HMS_THRIFT_PORT DATAHUB_GMS_URL DATAHUB_GMS_TOKEN
#   HIVE_INGEST_PLATFORM_INSTANCE — 已写死为与血缘一致：recipe 内 platform_instance=blf-prod-hive
#   LINEAGE_PYTHON / HIVE_INGEST_PYTHON — 解释器（与血缘任务一致时建议 /opt/anaconda3/bin/python）
#   TZ
#
# 搜不到表时排查:
#   1) DATAHUB_GMS_URL 必须与浏览器里 DataHub 实际连的 GMS 一致；127.0.0.1 仅在本机即 GMS 同机时有效。
#   2) 若 GMS 开启鉴权，必须 export DATAHUB_GMS_TOKEN。
#   3) UI 中环境选 PROD（与 recipe 中 env 一致）；Hive「库」在 DataHub 里多为容器/前缀 default。
#   4) 看日志末尾 entities produced / sink 是否报错；DH_INGEST_EXIT 非 0 表示未写入成功。
set -euo pipefail

RECIPE_DIR=/data/datahub/recipes
RECIPE_NAME=hive_ingest_one_database.yml
HOST_RECIPE="$RECIPE_DIR/$RECIPE_NAME"

DB="${1:-${HIVE_INGEST_DATABASE:-}}"
if [[ -z "$DB" ]]; then
  echo "用法: $0 <Hive库名>" >&2
  echo "  或: export HIVE_INGEST_DATABASE=<库名> && $0" >&2
  exit 1
fi
if ! [[ "$DB" =~ ^[a-zA-Z][a-zA-Z0-9_]*$ ]]; then
  echo "ERROR: 非法库名（须字母开头，仅字母数字下划线）: $DB" >&2
  exit 1
fi
export HIVE_INGEST_DATABASE="$DB"

export HMS_THRIFT_HOST="${HMS_THRIFT_HOST:-hiveserver5.dp.data.bj1.wormpex.com}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"
export TZ="${TZ:-Asia/Shanghai}"

if [[ -n "${HIVE_INGEST_PYTHON:-}" ]]; then
  PYTHON="$HIVE_INGEST_PYTHON"
elif [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

if [[ ! -f "$HOST_RECIPE" ]]; then
  echo "ERROR: recipe 不存在: $HOST_RECIPE" >&2
  exit 1
fi

export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
export PYTHONUNBUFFERED=1

if ! "$PYTHON" -c "import datahub" >/dev/null 2>&1; then
  echo "ERROR: 未安装 DataHub CLI: $PYTHON" >&2
  echo "  执行: $PYTHON -m pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'" >&2
  exit 1
fi
if ! "$PYTHON" -c "import datahub_classify" >/dev/null 2>&1; then
  echo "ERROR: 缺少 datahub_classify: $PYTHON" >&2
  echo "  执行: $PYTHON -m pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'" >&2
  exit 1
fi

echo "[INFO] ingest hive db=$HIVE_INGEST_DATABASE PYTHON=$PYTHON (host) recipe=$HOST_RECIPE GMS=$DATAHUB_GMS_URL"
if [[ "${DATAHUB_GMS_URL}" == *"127.0.0.1"* ]] || [[ "${DATAHUB_GMS_URL}" == *"localhost"* ]]; then
  echo "[WARN] GMS 指向本机环回地址；若你在浏览器打开的是其它地址的 DataHub，元数据写入了另一套 GMS，页面上会搜不到。" >&2
fi
if [[ -z "${DATAHUB_GMS_TOKEN:-}" ]]; then
  echo "[INFO] DATAHUB_GMS_TOKEN 未设置（无鉴权 GMS 可忽略）"
fi
set +e
"$PYTHON" -m datahub ingest -c "$HOST_RECIPE"
ec=$?
set -e
echo "DH_INGEST_EXIT=$ec"
exit "$ec"
