#!/usr/bin/env bash
# 从 Excel（.xlsx）导出每行 db.table 文本，供 HIVE_INGEST_TABLE_LIST_FILE 使用。
#
# 依赖: pip install openpyxl
#
# Jenkins 示例（Excel 已放到服务器，如 /data/datahub/in/20260512-hive-tables.xlsx）:
#   export HIVE_META_DT=20260512
#   export HIVE_XLSX_IN=/data/datahub/in/20260512-hive-tables.xlsx
#   export HIVE_TABLE_LIST_OUT=/data/datahub/out/hive_table_list_${HIVE_META_DT}.txt
#   export HIVE_INGEST_TABLE_LIST_FILE="${HIVE_TABLE_LIST_OUT}"
#   export LINEAGE_PYTHON=/opt/anaconda3/bin/python
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   bash /data/datahub/scripts/export_hive_table_list_from_xlsx.sh "${HIVE_XLSX_IN}" "${HIVE_TABLE_LIST_OUT}"
#   bash /data/datahub/scripts/ingest_hive_table_list_to_datahub.sh
#
# 用法（dim 导出 Excel：首行为 db_id,db_name,table_id,table_name,fqtn，默认 auto 读 fqtn 列）:
#   ./export_hive_table_list_from_xlsx.sh /path/in.xlsx /data/datahub/out/list.txt
#
# 双列（第 1 列库名、第 2 列表名）:
#   export HIVE_XLSX_DB_COL=1 HIVE_XLSX_TABLE_COL=2
#   ./export_hive_table_list_from_xlsx.sh /path/in.xlsx /path/out.txt
#
# 其它:
#   HIVE_XLSX_SHEET   工作表名或 0-based 索引，默认 0
#   HIVE_XLSX_FQTN_COL  单列模式：列号 1=A，或 auto（默认 auto，识别 fqtn 表头）
#   HIVE_XLSX_MAX_ROWS  试跑最大行数（可选）
#   HIVE_XLSX_EXCLUDE_DBS  逗号分隔要排除的库名，默认 data_finance；设为空字符串则不排除
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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

IN="${1:-${HIVE_XLSX_IN:-}}"
OUT="${2:-${HIVE_TABLE_LIST_OUT:-}}"
if [[ -z "$IN" ]] || [[ -z "$OUT" ]]; then
  echo "用法: $0 <输入.xlsx> <输出.txt>" >&2
  echo "  或: export HIVE_XLSX_IN=... HIVE_TABLE_LIST_OUT=... && $0" >&2
  exit 1
fi

if ! "$PYTHON" -c "import openpyxl" >/dev/null 2>&1; then
  echo "ERROR: 未安装 openpyxl: $PYTHON -m pip install openpyxl" >&2
  exit 1
fi

ARGS=(
  "$SCRIPT_DIR/export_hive_table_list_from_xlsx.py"
  --in "$IN"
  --out "$OUT"
  --sheet "${HIVE_XLSX_SHEET:-0}"
  --fqtn-col "${HIVE_XLSX_FQTN_COL:-auto}"
)

if [[ -n "${HIVE_XLSX_DB_COL:-}" ]] && [[ -n "${HIVE_XLSX_TABLE_COL:-}" ]]; then
  ARGS+=(--db-col "$HIVE_XLSX_DB_COL" --table-col "$HIVE_XLSX_TABLE_COL")
fi

if [[ -n "${HIVE_XLSX_MAX_ROWS:-}" ]]; then
  ARGS+=(--max-rows "$HIVE_XLSX_MAX_ROWS")
fi

if [[ -n "${HIVE_XLSX_EXCLUDE_DBS+x}" ]]; then
  ARGS+=(--exclude-dbs "$HIVE_XLSX_EXCLUDE_DBS")
fi

exec "$PYTHON" "${ARGS[@]}"
