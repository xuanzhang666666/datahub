#!/usr/bin/env bash
# run_algorithm_field_contract.sh — Jenkins: export A-side algorithm field contract
#
# Jenkins 参数:
#   TABLE_NAMES  Multi-line String Parameter, one Hive table per line.
#
# 可选环境变量:
#   DATAHUB_GMS_URL
#   DATAHUB_GMS_TOKEN
#   BLF_DATAHUB_PLATFORM_INSTANCE
#   DATAHUB_ENV
#   FIELD_CONTRACT_MAX_DEPTH
#   FIELD_CONTRACT_OUTPUT_DIR
#   LINEAGE_PYTHON
#   LINEAGE_ENV_FILE
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -d "${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}" ]]; then
  PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
  PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"
elif [[ -d "$SCRIPT_DIR/../job_info_sync_datahub" ]]; then
  PKG_DIR="$(cd "$SCRIPT_DIR/../job_info_sync_datahub" && pwd)"
  PYTHONPATH_ROOT="$(dirname "$PKG_DIR")"
else
  echo "ERROR: 找不到 job_info_sync_datahub，请设置 JOB_INFO_SYNC_DIR。" >&2
  exit 2
fi

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
    # shellcheck disable=SC1090
    set -a
    source "$_cand"
    set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

TABLE_NAMES="${TABLE_NAMES:-}"
if [[ -z "${TABLE_NAMES//[[:space:]]/}" ]]; then
  echo "ERROR: TABLE_NAMES 为空，请在 Jenkins Multi-line String Parameter 中填写目标表名。" >&2
  exit 2
fi

GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
REPORT_WORKSPACE="${WORKSPACE:-$PWD}"
OUTPUT_DIR="${FIELD_CONTRACT_OUTPUT_DIR:-$REPORT_WORKSPACE/algorithm_field_contract_$(date +%Y%m%d_%H%M%S)}"
MAX_DEPTH="${FIELD_CONTRACT_MAX_DEPTH:-3}"

EXTRA_ARGS=()
if [[ -n "${DATAHUB_GMS_TOKEN:-}" ]]; then
  EXTRA_ARGS+=(--token "$DATAHUB_GMS_TOKEN")
fi
if [[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]]; then
  EXTRA_ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
fi
if [[ -n "${DATAHUB_ENV:-}" ]]; then
  EXTRA_ARGS+=(--env "$DATAHUB_ENV")
fi

mkdir -p "$OUTPUT_DIR"

echo "[INFO] algorithm field contract started at $(date -Iseconds)"
echo "[INFO] gms url: $GMS_URL"
echo "[INFO] python: $PYTHON"
echo "[INFO] max depth: $MAX_DEPTH"
echo "[INFO] output dir: $OUTPUT_DIR"
echo "[INFO] table names:"
printf '%s\n' "$TABLE_NAMES"
echo "[INFO] ----------------------------------------"

(
  cd "$PYTHONPATH_ROOT"
  PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" -m job_info_sync_datahub.algorithm_field_contract \
    --gms-url "$GMS_URL" \
    --max-depth "$MAX_DEPTH" \
    --output-dir "$OUTPUT_DIR" \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
)

echo "[INFO] ----------------------------------------"
echo "[INFO] algorithm field contract finished at $(date -Iseconds)"
echo "[INFO] outputs:"
echo "  $OUTPUT_DIR/field_contract.xlsx"
echo "  $OUTPUT_DIR/field_lineage.json"
echo "  $OUTPUT_DIR/runtime_context.json"
echo "  $OUTPUT_DIR/lineage_report.md"
echo "  $OUTPUT_DIR/open_questions.md"
