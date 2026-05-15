#!/usr/bin/env bash
# 将 Hive 表清单 xlsx 切分为多个小 xlsx。
# 默认按 **Hive 库** 各一个文件（*.bydb.*.xlsx），并默认排除 data_finance。
# 旧行为（按固定行数）: export HIVE_XLSX_SPLIT_MODE=rows
#
# 用法:
#   ./split_hive_tables_xlsx.sh /path/in.xlsx /path/out_dir
# 或:
#   export HIVE_XLSX_IN=... HIVE_XLSX_CHUNK_DIR=...
#   ./split_hive_tables_xlsx.sh
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
OUT_DIR="${2:-${HIVE_XLSX_CHUNK_DIR:-}}"
if [[ -z "$IN" ]] || [[ -z "$OUT_DIR" ]]; then
  echo "用法: $0 <输入.xlsx> <输出目录>" >&2
  echo "  或: export HIVE_XLSX_IN=... HIVE_XLSX_CHUNK_DIR=... && $0" >&2
  exit 1
fi

if ! "$PYTHON" -c "import openpyxl" >/dev/null 2>&1; then
  echo "ERROR: pip install openpyxl" >&2
  exit 1
fi

MODE="${HIVE_XLSX_SPLIT_MODE:-db}"
ARGS=(
  "$SCRIPT_DIR/split_hive_tables_xlsx.py"
  --in "$IN"
  --out-dir "$OUT_DIR"
  --mode "$MODE"
)
if [[ -n "${HIVE_XLSX_CHUNK_PREFIX:-}" ]]; then
  ARGS+=(--prefix "$HIVE_XLSX_CHUNK_PREFIX")
fi
if [[ -n "${HIVE_XLSX_EXCLUDE_DBS+x}" ]]; then
  ARGS+=(--exclude-dbs "$HIVE_XLSX_EXCLUDE_DBS")
fi
if [[ "$MODE" == "rows" ]]; then
  ARGS+=(--chunk-size "${HIVE_XLSX_CHUNK_ROWS:-1000}")
fi

exec "$PYTHON" "${ARGS[@]}"
