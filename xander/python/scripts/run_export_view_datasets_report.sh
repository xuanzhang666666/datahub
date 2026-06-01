#!/usr/bin/env bash
# run_export_view_datasets_report.sh — 导出 DataHub Hive view 清单（名称 / 上游数 / flag / View Definition）
#
# 环境变量：
#   REPORT_DIR              报告目录（默认 $WORKSPACE/view_reports 或脚本旁 view_reports）
#   TABLE_PRE               可选，只导出表名前缀匹配的 view（如 pdw）
#   BLF_DATAHUB_PLATFORM_INSTANCE  默认 blf-prod-hive
#   DATAHUB_ENV             默认 PROD
#   LINEAGE_PYTHON          Python 解释器
#   DATAHUB_MYSQL_MODE      host | docker（与其它 xander 脚本一致）
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
  OUT_DIR="$WORKSPACE/view_reports"
else
  OUT_DIR="$SCRIPT_DIR/view_reports"
fi
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
CSV_OUT="$OUT_DIR/view_datasets_${STAMP}.csv"
XLSX_OUT="$OUT_DIR/view_datasets_${STAMP}.xlsx"

ARGS=(--output-csv "$CSV_OUT" --output-xlsx "$XLSX_OUT")
[[ -n "${TABLE_PRE:-}" ]] && ARGS+=(--table-prefix "$TABLE_PRE")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS+=(--env "$DATAHUB_ENV")

echo "==================================================================="
echo " Export Hive view datasets from DataHub"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " OUT_DIR=$OUT_DIR"
echo " TABLE_PRE=${TABLE_PRE:-<all>}"
echo "==================================================================="

# Avoid importing stale /root/job_info_sync_datahub when bastion cwd is $HOME.
cd "$PYTHONPATH_ROOT"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 \
  "$PYTHON" -m job_info_sync_datahub.export_view_datasets_report "${ARGS[@]}"

echo "[INFO] latest csv: $CSV_OUT"
echo "[INFO] latest xlsx: $XLSX_OUT"
