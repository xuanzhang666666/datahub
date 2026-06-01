#!/usr/bin/env bash
# run_batch_update_view_availability_flags.sh
#
# 筛选 view：upstream_count>0 且 View Definition 有内容（是），
# 将 data_availability_flag 更新为 DDL + 表血缘 + 字段血缘。
#
# 环境变量：
#   VIEW_EXPORT_CSV   可选；export_view 的 CSV。为空则从 MySQL 重新扫描 view
#   DRY_RUN           1=只出报告不写 GMS（默认 1）；0=写入
#   CONCURRENCY       并发（默认 10）
#   REPORT_DIR        报告目录
#   TABLE_PRE         无 CSV 时限制 view 表名前缀
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
    # shellcheck disable=SC1090
    set -a; source "$_cand"; set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

if [[ -n "${REPORT_DIR:-}" ]]; then
  OUT_DIR="$REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  OUT_DIR="$WORKSPACE/view_availability_update_reports"
else
  OUT_DIR="$SCRIPT_DIR/view_availability_update_reports"
fi
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
JSONL_OUT="$OUT_DIR/view_flag_update_${STAMP}.jsonl"
XLSX_OUT="$OUT_DIR/view_flag_update_${STAMP}.xlsx"
ELIGIBLE_LIST="$OUT_DIR/eligible_views_${STAMP}.txt"

ARGS=(
  --jsonl "$JSONL_OUT"
  --xlsx "$XLSX_OUT"
  --eligible-list "$ELIGIBLE_LIST"
  --concurrency "${CONCURRENCY:-10}"
)
[[ -n "${VIEW_EXPORT_CSV:-}" ]] && ARGS+=(--view-export-csv "$VIEW_EXPORT_CSV")
[[ -n "${TABLE_PRE:-}" ]] && ARGS+=(--table-prefix "$TABLE_PRE")
[[ -n "${DATAHUB_GMS_URL:-}" ]] && ARGS+=(--datahub-gms "$DATAHUB_GMS_URL")
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS+=(--token "$DATAHUB_GMS_TOKEN")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS+=(--env "$DATAHUB_ENV")
[[ "${DRY_RUN:-1}" == "1" ]] && ARGS+=(--dry-run)

echo "==================================================================="
echo " Batch update view data_availability_flag"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " OUT_DIR=$OUT_DIR"
echo " VIEW_EXPORT_CSV=${VIEW_EXPORT_CSV:-<mysql scan>}"
echo " DRY_RUN=${DRY_RUN:-1}"
echo " target_flags=DDL,表血缘,字段血缘"
echo "==================================================================="

cd "$PYTHONPATH_ROOT"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 \
  "$PYTHON" -m job_info_sync_datahub.batch_update_view_availability_flags "${ARGS[@]}"

echo "[INFO] eligible list: $ELIGIBLE_LIST"
echo "[INFO] jsonl: $JSONL_OUT"
echo "[INFO] xlsx: $XLSX_OUT"
