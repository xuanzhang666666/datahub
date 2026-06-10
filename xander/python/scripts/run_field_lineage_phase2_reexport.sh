#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

# run_field_lineage_phase2_reexport.sh — 从 batch audit JSONL 批量重导出 Phase2 脏表字段血缘
#
# 用法（neo4j2）:
#   PHASE2_AUDIT_JSONL=/root/field_lineage_batch_audit.jsonl \
#   PHASE2_TIER=phase2_reexport_llm \
#   sh /data/datahub/scripts/run_field_lineage_phase2_reexport.sh
#
# PHASE2_TIER 可选:
#   phase2_reexport_llm          — tmp/CTE 类，默认先跑这批（49 张）
#   phase2_reexport_or_manual    — JSON/表达式等（165 张）
#   all                          — 上述两批 + Phase1 剩余 9 张自引用表
#
# 关键环境变量（传给 run_field_lineage_export_to_excel.sh）:
#   FIELD_LINEAGE_FORCE_REFRESH=1
#   FIELD_LINEAGE_AUTO_IMPORT_CLEAR_EXISTING=1
#   FIELD_LINEAGE_AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED=0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXPORT_SCRIPT="$SCRIPT_DIR/run_field_lineage_export_to_excel.sh"
AUDIT_JSONL="${PHASE2_AUDIT_JSONL:-/root/field_lineage_batch_audit.jsonl}"
PHASE2_TIER="${PHASE2_TIER:-phase2_reexport_llm}"
PHASE2_LIMIT="${PHASE2_LIMIT:-0}"
SELF_REF_FILE="${PHASE2_SELF_REF_FILE:-/root/phase1_self_ref_tables.txt}"

if [[ ! -f "$EXPORT_SCRIPT" ]]; then
  echo "ERROR: 找不到 $EXPORT_SCRIPT" >&2
  exit 2
fi
if [[ ! -f "$AUDIT_JSONL" ]]; then
  echo "ERROR: audit JSONL 不存在: $AUDIT_JSONL" >&2
  exit 2
fi

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
else
  PYTHON=python3
fi

TABLE_LIST_FILE="$(mktemp "${TMPDIR:-/tmp}/phase2_tables.XXXXXX.txt")"
cleanup() {
  rm -f "$TABLE_LIST_FILE"
}
trap cleanup EXIT

"$PYTHON" - <<'PY' "$AUDIT_JSONL" "$PHASE2_TIER" "$PHASE2_LIMIT" "$TABLE_LIST_FILE" "$SELF_REF_FILE"
import json
import sys
from pathlib import Path

audit_path, tier, limit_raw, out_path, self_ref_path = sys.argv[1:6]
limit = int(limit_raw or "0")
seen: set[str] = set()
tables: list[str] = []

if tier == "all":
    wanted = {"phase2_reexport_llm", "phase2_reexport_or_manual"}
else:
    wanted = {tier}

for line in Path(audit_path).read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    row = json.loads(line)
    if row.get("repair_tier") not in wanted:
        continue
    table = str(row.get("table", "")).strip()
    if not table or table in seen:
        continue
    seen.add(table)
    tables.append(table)

if tier == "all" and Path(self_ref_path).is_file():
    for line in Path(self_ref_path).read_text(encoding="utf-8").splitlines():
        table = line.strip()
        if table and table not in seen:
            seen.add(table)
            tables.append(table)

if limit > 0:
    tables = tables[:limit]

Path(out_path).write_text("\n".join(tables) + ("\n" if tables else ""), encoding="utf-8")
print(f"[INFO] phase2 tier={tier} table_count={len(tables)}")
for table in tables[:10]:
    print(f"[INFO]   {table}")
if len(tables) > 10:
    print(f"[INFO]   ... and {len(tables) - 10} more")
PY

if [[ ! -s "$TABLE_LIST_FILE" ]]; then
  echo "ERROR: 未从 $AUDIT_JSONL 解析到 Phase2 表（tier=$PHASE2_TIER）" >&2
  exit 2
fi

export TABLES
TABLES="$(cat "$TABLE_LIST_FILE")"
export FIELD_LINEAGE_FORCE_REFRESH=1
export FIELD_LINEAGE_AUTO_IMPORT_CLEAR_EXISTING=1
export FIELD_LINEAGE_AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED=0
export FIELD_LINEAGE_CONCURRENCY="${FIELD_LINEAGE_CONCURRENCY:-6}"

echo "[INFO] phase2 re-export starting tier=$PHASE2_TIER tables=$(wc -l <"$TABLE_LIST_FILE" | tr -d ' ')"
exec bash "$EXPORT_SCRIPT"
