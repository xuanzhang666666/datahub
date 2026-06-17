#!/usr/bin/env bash
# run_scheduler_datajob_sync_daily.sh — 每日全量同步调度 job 元数据到 DataHub
#
# 同步内容：DataJob 基础属性、customProperties、上游血缘（content XML 优先）、
#           shell 脚本 / job XML structuredProperties。
#
# 调度建议：Timer 每天一次，例如 cron spec: 0 3 * * *
#
# 环境变量（可选）：
#   SCHEDULER_DATAJOB_SYNC_DIR  scheduler_datajob_sync 包所在目录的父路径，默认脚本目录
#   LINEAGE_PYTHON              Python 解释器，默认 /opt/anaconda3/bin/python
#   SCHEDULER_SYNC_LOG_DIR      日志目录，默认 $SCRIPT_DIR/logs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_ROOT="${SCHEDULER_DATAJOB_SYNC_DIR:-$SCRIPT_DIR}"
LOG_DIR="${SCHEDULER_SYNC_LOG_DIR:-$SYNC_ROOT/logs}"
LOG_FILE="$LOG_DIR/scheduler_datajob_sync_daily_$(date +%Y%m%d).log"
ENV_FILE="$SYNC_ROOT/lineage.env"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ -x /opt/anaconda3/bin/python3 ]]; then
  PYTHON=/opt/anaconda3/bin/python3
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

mkdir -p "$LOG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[error] missing env file: $ENV_FILE" | tee -a "$LOG_FILE"
  exit 1
fi

# Jenkins string parameters are already in the environment; save them before
# sourcing lineage.env so that lineage.env cannot accidentally overwrite them.
_SAVED_MIN_BATCH_EXEC_TIME="${SCHEDULER_MIN_BATCH_EXEC_TIME:-}"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Restore Jenkins parameter if it was provided (takes precedence over lineage.env).
if [[ -n "$_SAVED_MIN_BATCH_EXEC_TIME" ]]; then
  export SCHEDULER_MIN_BATCH_EXEC_TIME="$_SAVED_MIN_BATCH_EXEC_TIME"
fi
unset _SAVED_MIN_BATCH_EXEC_TIME

cd "$SYNC_ROOT"
export PYTHONPATH="$SYNC_ROOT"

echo "===================================================================" | tee -a "$LOG_FILE"
echo "[start] $(date -Is) scheduler DataJob full sync" | tee -a "$LOG_FILE"
echo " sync_root=$SYNC_ROOT" | tee -a "$LOG_FILE"
echo " python=$PYTHON" | tee -a "$LOG_FILE"
echo " log=$LOG_FILE" | tee -a "$LOG_FILE"
echo " SCHEDULER_MIN_BATCH_EXEC_TIME=${SCHEDULER_MIN_BATCH_EXEC_TIME:-(not set, using default in mysql_client.py)}" | tee -a "$LOG_FILE"
echo "===================================================================" | tee -a "$LOG_FILE"

"$PYTHON" -u -m scheduler_datajob_sync.sync_datajobs \
  --updated-since 1970-01-01T00:00:00 \
  2>&1 | tee -a "$LOG_FILE"
sync_status=${PIPESTATUS[0]}

echo "-------------------------------------------------------------------" | tee -a "$LOG_FILE"
echo "[cleanup] $(date -Is) removing stale DataHub dataJobs …" | tee -a "$LOG_FILE"
"$PYTHON" -u -m scheduler_datajob_sync.cleanup_stale_datajobs --apply \
  2>&1 | tee -a "$LOG_FILE"
cleanup_status=${PIPESTATUS[0]}

# Report overall exit status: non-zero if either step failed
if [[ "$sync_status" -ne 0 ]]; then
  status="$sync_status"
elif [[ "$cleanup_status" -ne 0 ]]; then
  status="$cleanup_status"
else
  status=0
fi
echo "[exit] $(date -Is) status=$status log=$LOG_FILE" | tee -a "$LOG_FILE"
exit "$status"
