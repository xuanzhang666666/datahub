#!/usr/bin/env bash
# debug_table_lineage_from_dataset_props.sh — 单表血缘：打印 LLM / fqtn / Hive 各阶段上游差异
#
# 用法（neo4j2 / Jenkins 节点）:
#   export TABLE_NAME=data_takeaway.pdw_takeaway_store_operating_state_info_di
#   export DATAHUB_GMS_URL=http://localhost:8080
#   export DRY_RUN=1
#   sh /data/datahub/scripts/debug_table_lineage_from_dataset_props.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
else
  PYTHON=python3
fi

for _cand in "${LINEAGE_ENV_FILE:-}" "$SCRIPT_DIR/lineage.env" ${WORKSPACE:+"$WORKSPACE/lineage.env"}; do
  [[ -z "$_cand" ]] && continue
  if [[ -r "$_cand" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$_cand"
    set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

TABLE_NAME="${TABLE_NAME:-}"
if [[ -z "${TABLE_NAME//[[:space:]]/}" ]]; then
  echo "ERROR: 请设置 TABLE_NAME=库.表" >&2
  exit 2
fi

REPORT_DIR="${REPORT_DIR:-${WORKSPACE:-$(pwd)}/lineage_reports}"
mkdir -p "$REPORT_DIR"
TABLE_FILE="$REPORT_DIR/debug_single_table.txt"
printf '%s\n' "$TABLE_NAME" > "$TABLE_FILE"
BATCH_REPORT="$REPORT_DIR/debug_table_lineage_report.jsonl"
AUDIT_JSONL="$REPORT_DIR/debug_table_lineage_audit.jsonl"

ARGS=(
  --table-file "$TABLE_FILE"
  --report "$BATCH_REPORT"
  --audit-jsonl "$AUDIT_JSONL"
  --dry-run
  --concurrency 1
)
[[ -n "${DATAHUB_GMS_URL:-}" ]] && ARGS+=(--datahub-gms "$DATAHUB_GMS_URL")
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS+=(--token "$DATAHUB_GMS_TOKEN")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS+=(--env "$DATAHUB_ENV")
[[ -n "${LLM_TIMEOUT:-}" ]] && ARGS+=(--llm-timeout "$LLM_TIMEOUT")

echo "[INFO] debug single table lineage: $TABLE_NAME"
cd "$PYTHONPATH_ROOT"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 \
  "$PYTHON" -m job_info_sync_datahub.table_lineage_from_dataset_props "${ARGS[@]}"

echo "[INFO] audit jsonl: $AUDIT_JSONL"
echo "[INFO] 对比 sources_deepseek vs sources_chosen，以及 report 中 lineage_dropped_by_* 字段"
