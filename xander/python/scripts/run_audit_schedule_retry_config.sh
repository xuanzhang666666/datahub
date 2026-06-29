#!/usr/bin/env bash
# 从 DataHub 的 DataJob Job XML 中检查 Naginator FixedDelay 配置。
set -euo pipefail

source /data/datahub/scripts/lineage.env

PYTHON="${LINEAGE_PYTHON:-/opt/anaconda3/bin/python}"
GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
REPORT_ROOT="${WORKSPACE:-/data/datahub/reports}"
OUTPUT_DIR="${RETRY_AUDIT_OUTPUT_DIR:-$REPORT_ROOT/schedule_retry_audit_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$OUTPUT_DIR"

ARGS=(
    /data/datahub/scripts/audit_schedule_retry_config.py
    --gms-url "$GMS_URL"
    --output-dir "$OUTPUT_DIR"
)
if [[ -n "${DATAHUB_GMS_TOKEN:-}" ]]; then
    ARGS+=(--token "$DATAHUB_GMS_TOKEN")
fi

echo "[INFO] schedule retry audit started at $(date -Iseconds)"
echo "[INFO] gms url: $GMS_URL"
echo "[INFO] output dir: $OUTPUT_DIR"

"$PYTHON" "${ARGS[@]}"

echo "[INFO] schedule retry audit finished at $(date -Iseconds)"
