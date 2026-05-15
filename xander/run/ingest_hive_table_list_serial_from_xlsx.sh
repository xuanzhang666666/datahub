#!/usr/bin/env bash
# 将大 xlsx 先切分为多个小 xlsx（默认 **按 Hive 库** 各一个 *.bydb.*.xlsx，并排除 data_finance），
# 再串行：导出 txt -> datahub ingest。
#
# 依赖: openpyxl、DataHub CLI（与 export_hive / ingest 脚本一致）
#
# 用法:
#   export HIVE_XLSX_IN=/data/datahub/in/20260512-hive-tables.xlsx
#   export HIVE_XLSX_CHUNK_DIR=/data/datahub/out/xlsx_chunks_20260512
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   export LINEAGE_PYTHON=/opt/anaconda3/bin/python
#   bash /data/datahub/scripts/ingest_hive_table_list_serial_from_xlsx.sh
#
# 可选:
#   HIVE_XLSX_SPLIT_MODE   db（默认，按库）或 rows（按行数，每块 HIVE_XLSX_CHUNK_ROWS）
#   HIVE_XLSX_CHUNK_ROWS   mode=rows 时每块数据行数，默认 1000
#   HIVE_XLSX_CHUNK_PREFIX 输出文件名前缀，默认用输入文件 stem
#   HIVE_XLSX_EXCLUDE_DBS  逗号分隔排除库，默认 data_finance；设为空字符串不排除
#   HIVE_INGEST_QUIET      默认 1（少日志）
#   HIVE_INGEST_DB_NAME    只 ingest 指定库（传给 ingest 脚本第一个参数）
#   HIVE_INGEST_INCLUDE_VIEW_LINEAGE  默认 1（开启视图 SQL 血缘）；设为 0 关闭以加速
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

IN="${1:-${HIVE_XLSX_IN:-}}"
CHUNK_DIR="${2:-${HIVE_XLSX_CHUNK_DIR:-}}"
if [[ -z "$IN" ]] || [[ ! -f "$IN" ]]; then
  echo "ERROR: 请设置 HIVE_XLSX_IN 或传入第一个参数为输入 xlsx" >&2
  exit 1
fi
if [[ -z "$CHUNK_DIR" ]]; then
  stem=$(basename "$IN" .xlsx)
  CHUNK_DIR="${HIVE_XLSX_CHUNK_DIR_DEFAULT:-/data/datahub/out}/xlsx_chunks_${stem}"
fi

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

export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
export PYTHONUNBUFFERED=1
# 串行 Excel 默认开启视图血缘（单文件 ingest 脚本默认仍为关）
export HIVE_INGEST_INCLUDE_VIEW_LINEAGE="${HIVE_INGEST_INCLUDE_VIEW_LINEAGE:-1}"

SPLIT_MODE="${HIVE_XLSX_SPLIT_MODE:-db}"
mkdir -p "$CHUNK_DIR"
echo "[INFO] split xlsx -> $CHUNK_DIR (mode=${SPLIT_MODE} rows_per_chunk=${HIVE_XLSX_CHUNK_ROWS:-1000}) view_lineage=${HIVE_INGEST_INCLUDE_VIEW_LINEAGE}"
export HIVE_XLSX_IN="$IN"
export HIVE_XLSX_CHUNK_DIR="$CHUNK_DIR"
export HIVE_XLSX_SPLIT_MODE="$SPLIT_MODE"
bash "$SCRIPT_DIR/split_hive_tables_xlsx.sh"

shopt -s nullglob
if [[ "$SPLIT_MODE" == "rows" ]]; then
  parts=( "$CHUNK_DIR"/*.part*.xlsx )
else
  parts=( "$CHUNK_DIR"/*.bydb.*.xlsx )
fi
shopt -u nullglob
if [[ ${#parts[@]} -eq 0 ]]; then
  echo "ERROR: 未找到切分后的 xlsx 于 $CHUNK_DIR（mode=$SPLIT_MODE）" >&2
  exit 1
fi

IFS=$'\n' sorted=( $(printf '%s\n' "${parts[@]}" | sort) )
unset IFS

n=0
for part in "${sorted[@]}"; do
  n=$((n + 1))
  base=$(basename "$part" .xlsx)
  list_tmp="${CHUNK_DIR}/${base}.txt"
  recipe_tmp="/tmp/hive_ingest.${base}.generated.yml"
  echo ""
  echo "========== [SERIAL $n / ${#sorted[@]}] $part =========="

  export HIVE_INGEST_RECIPE_OUT="$recipe_tmp"
  bash "$SCRIPT_DIR/export_hive_table_list_from_xlsx.sh" "$part" "$list_tmp"

  export HIVE_INGEST_TABLE_LIST_FILE="$list_tmp"
  set +e
  if [[ -n "${HIVE_INGEST_DB_NAME:-}" ]]; then
    bash "$SCRIPT_DIR/ingest_hive_table_list_to_datahub.sh" "${HIVE_INGEST_DB_NAME}"
  else
    bash "$SCRIPT_DIR/ingest_hive_table_list_to_datahub.sh"
  fi
  ec=$?
  set -e
  echo "DH_CHUNK_EXIT($base)=$ec"
  if [[ "$ec" -ne 0 ]]; then
    echo "[ERROR] 本块失败，停止后续块。可修复后从本 xlsx 重跑或删除已成功块后重试。" >&2
    exit "$ec"
  fi
done

echo "[INFO] 全部 ${#sorted[@]} 块完成。"
exit 0
