#!/usr/bin/env bash
# 从 Trino 查询 dim_dc_hive_meta_info_di，导出每行 db.table 列表文件。
#
# Jenkins / 非 root 注意:
#   /data/datahub/scripts 属主多为 root:755，slave（如 iuser）无法在该目录新建文件。
#   请把列表写到可写目录，例如（neo4j2 上已建 chmod 1777）:
#     export HIVE_TABLE_LIST_OUT=/data/datahub/out/hive_table_list_${HIVE_META_DT}.txt
#   再 export HIVE_INGEST_TABLE_LIST_FILE 指向同一路径跑 ingest。
#
# 用法:
#   export HIVE_META_DT=20260512
#   export HIVE_TABLE_LIST_OUT=/data/datahub/out/hive_table_list_20260512.txt
#   ./export_hive_table_list_from_dim_meta.sh
#
# 或:
#   ./export_hive_table_list_from_dim_meta.sh 20260512 /data/datahub/out/tables.txt
#
# 环境变量（与 schedule_client 默认一致）: TRINO_HOST TRINO_PORT TRINO_USER TRINO_CATALOG TRINO_SCHEMA
# 依赖: pip install trino
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

DT="${1:-${HIVE_META_DT:-}}"
OUT="${2:-${HIVE_TABLE_LIST_OUT:-}}"
if [[ -z "$DT" ]] || [[ -z "$OUT" ]]; then
  echo "用法: $0 <dt> <输出文件路径>" >&2
  echo "  或: export HIVE_META_DT=20260512 HIVE_TABLE_LIST_OUT=/path/list.txt && $0" >&2
  exit 1
fi

if ! "$PYTHON" -c "import trino" >/dev/null 2>&1; then
  echo "ERROR: 未安装 trino: $PYTHON -m pip install trino" >&2
  exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/export_hive_table_list_from_dim_meta.py" --dt "$DT" --out "$OUT"
