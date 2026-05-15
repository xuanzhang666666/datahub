#!/usr/bin/env bash
# 按「表名列表」从 HMS 同步元数据到 DataHub（生成临时 recipe 后 ingest）。
# 适合 7w+ 表：列表按块合并为正则，避免整库扫描与单条超长正则。
#
# 用法:
#   export HIVE_INGEST_TABLE_LIST_FILE=/path/to/tables.txt
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   ./ingest_hive_table_list_to_datahub.sh
# 只跑某一个库（分库执行，列表里可仍含多库，脚本会按库过滤）:
#   ./ingest_hive_table_list_to_datahub.sh data_logistics
#   或: export HIVE_INGEST_DB_NAME=data_logistics
#
# 列表格式（每行一条，# 开头为注释）:
#   default.some_table
#   data_logistics.other_table
# 从 xlsx 批量导出列表见: xander/run/export_hive_table_list_from_xlsx.sh
#   或串行全流程: xander/run/ingest_hive_table_list_serial_from_xlsx.sh
#
# 若每行只有表名，需:
#   export HIVE_INGEST_IMPLICIT_DATABASE=default
#
# 可选环境变量:
#   HIVE_INGEST_CHUNK_SIZE     每条 allow 正则合并表数，默认 600
#   HMS_THRIFT_HOST HMS_THRIFT_PORT DATAHUB_GMS_TOKEN
#   LINEAGE_PYTHON / HIVE_INGEST_PYTHON
#   HIVE_INGEST_INCLUDE_VIEW_LINEAGE=1  — 默认关闭（大表量时视图解析很慢）
#   HIVE_INGEST_RECIPE_OUT       生成的 yaml 路径；指定分库时未设置则默认
#                                /tmp/hive_ingest_table_list.<库名>.generated.yml
#   HIVE_INGEST_DB_NAME          与第一个参数二选一：只 ingest 该库
#   HIVE_INGEST_DEBUG=1          传给 datahub --debug（日志极大，勿在 Jenkins 开）
#   HIVE_INGEST_QUIET=1          默认 1：ingest 加 --no-progress --no-spinner，减少 Jenkins 日志卡死
#                                排障时设 HIVE_INGEST_QUIET=0 恢复中间进度块
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_FILTER="${1:-${HIVE_INGEST_DB_NAME:-}}"
# 文件名安全片段（仅用于默认 recipe 路径）
SAFE_DB=""
if [[ -n "$DB_FILTER" ]]; then
  SAFE_DB=$(printf '%s' "$DB_FILTER" | sed 's/[^a-zA-Z0-9_-]/_/g')
fi
LIST_FILE="${HIVE_INGEST_TABLE_LIST_FILE:-}"
if [[ -z "$LIST_FILE" ]] || [[ ! -f "$LIST_FILE" ]]; then
  echo "ERROR: 请设置 HIVE_INGEST_TABLE_LIST_FILE 为存在的表列表文件" >&2
  exit 1
fi

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

RECIPE_OUT="${HIVE_INGEST_RECIPE_OUT:-}"
if [[ -z "$RECIPE_OUT" ]]; then
  if [[ -n "$SAFE_DB" ]]; then
    RECIPE_OUT="/tmp/hive_ingest_table_list.${SAFE_DB}.generated.yml"
  else
    RECIPE_OUT="/tmp/hive_ingest_table_list.generated.yml"
  fi
fi
CHUNK="${HIVE_INGEST_CHUNK_SIZE:-600}"

export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
export PYTHONUNBUFFERED=1

if ! "$PYTHON" -c "import datahub" >/dev/null 2>&1; then
  echo "ERROR: 需要 DataHub CLI: $PYTHON" >&2
  echo "  pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'" >&2
  exit 1
fi

RENDER_ARGS=(
  "$SCRIPT_DIR/render_hive_ingest_recipe.py"
  --table-list "$LIST_FILE"
  --out "$RECIPE_OUT"
  --chunk-size "$CHUNK"
)

if [[ -n "${HIVE_INGEST_IMPLICIT_DATABASE:-}" ]]; then
  RENDER_ARGS+=(--implicit-database "$HIVE_INGEST_IMPLICIT_DATABASE")
fi

if [[ "${HIVE_INGEST_INCLUDE_VIEW_LINEAGE:-}" == "1" ]]; then
  RENDER_ARGS+=(--include-view-lineage)
fi

if [[ -n "$DB_FILTER" ]]; then
  RENDER_ARGS+=(--database "$DB_FILTER")
fi

echo "[INFO] render recipe -> $RECIPE_OUT (list=$LIST_FILE chunk=$CHUNK db=${DB_FILTER:-全部} PYTHON=$PYTHON)"
"$PYTHON" "${RENDER_ARGS[@]}"

QUIET="${HIVE_INGEST_QUIET:-1}"
echo "[INFO] datahub ingest -c $RECIPE_OUT GMS=$DATAHUB_GMS_URL quiet=${QUIET}"
set +e
DH_CMD=("$PYTHON" -m datahub)
if [[ "${HIVE_INGEST_DEBUG:-}" == "1" ]]; then
  DH_CMD+=(--debug)
fi
DH_CMD+=(ingest -c "$RECIPE_OUT")
if [[ "$QUIET" == "1" ]]; then
  DH_CMD+=(--no-progress --no-spinner)
fi
"${DH_CMD[@]}"
ec=$?
set -e
echo "DH_INGEST_EXIT=$ec"
exit "$ec"
