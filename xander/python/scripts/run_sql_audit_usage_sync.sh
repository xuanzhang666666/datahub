#!/usr/bin/env bash
# run_sql_audit_usage_sync.sh
#
# 从 data_platform.hive_sql_audit / trino_query_history 生成 DataHub usage/operation 增强数据。
#
# 环境变量：
#   DATE              执行日期，格式 YYYY-MM-DD；默认昨天
#   ENGINE            hive | trino | both；默认 both
#   DRY_RUN           1=只出 JSON 报告（默认）；0=写入 DataHub
#   LIMIT             可选，限制每个来源读取行数，用于冒烟
#   REPORT_DIR        报告目录
#   OPERATION_CHECKPOINT_FILE  Operation 去重文件；默认按 DATE 放在 Jenkins workspace 下
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

RUN_DATE="${DATE:-$(date -d yesterday +%F)}"
ENGINE="${ENGINE:-both}"

if [[ -n "${REPORT_DIR:-}" ]]; then
  OUT_DIR="$REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  OUT_DIR="$WORKSPACE/sql_audit_usage_reports"
else
  OUT_DIR="$SCRIPT_DIR/sql_audit_usage_reports"
fi
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_JSON="$OUT_DIR/sql_audit_usage_${RUN_DATE}_${ENGINE}_${STAMP}.json"

if [[ -n "${OPERATION_CHECKPOINT_FILE:-}" ]]; then
  OP_CKPT="$OPERATION_CHECKPOINT_FILE"
elif [[ -n "${WORKSPACE:-}" ]]; then
  OP_CKPT="$WORKSPACE/sql_audit_usage_checkpoint/operation_keys_${RUN_DATE}.jsonl"
else
  OP_CKPT="$SCRIPT_DIR/sql_audit_usage_checkpoint/operation_keys_${RUN_DATE}.jsonl"
fi

ARGS=(
  --date "$RUN_DATE"
  --engine "$ENGINE"
  --output "$OUT_JSON"
  --operation-checkpoint "$OP_CKPT"
)
[[ -n "${LIMIT:-}" ]] && ARGS+=(--limit "$LIMIT")
[[ -n "${SCHEDULER_MYSQL_DATABASE:-}" ]] && ARGS+=(--database "$SCHEDULER_MYSQL_DATABASE")
[[ -n "${DATAHUB_GMS_URL:-}" ]] && ARGS+=(--gms-url "$DATAHUB_GMS_URL")
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS+=(--gms-token "$DATAHUB_GMS_TOKEN")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS+=(--env "$DATAHUB_ENV")
[[ "${DRY_RUN:-1}" == "0" ]] && ARGS+=(--emit)

echo "==================================================================="
echo " SQL audit usage sync"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " RUN_DATE=$RUN_DATE"
echo " ENGINE=$ENGINE"
echo " DRY_RUN=${DRY_RUN:-1}"
echo " OUT_JSON=$OUT_JSON"
echo " OPERATION_CHECKPOINT_FILE=$OP_CKPT"
echo "==================================================================="

cd "$PYTHONPATH_ROOT"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.sql_audit_usage "${ARGS[@]}"

echo "[INFO] report: $OUT_JSON"
