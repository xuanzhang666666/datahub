#!/usr/bin/env bash
# run_scheduler_datajob_sync_hourly.sh — 按运行/构建/批次时间增量同步调度 job 到 DataHub
#
# 筛选字段（任一满足即同步）：
#   last_build_start_time  上次构建开始时间
#   build_update_time      构建目录最近更新时间
#
# batch_exec_time 在同一元数据批次内对所有 job 相同，不适合小时级增量；
# 新批次全量同步请用 run_scheduler_datajob_sync_daily.sh。
#
# 调度建议：Timer 每小时一次，例如 cron spec: 0 * * * *
#
# 环境变量（可选）：
#   SCHEDULER_DATAJOB_SYNC_DIR  代码根目录，默认脚本目录
#   LINEAGE_PYTHON              Python 解释器
#   SCHEDULER_SYNC_LOG_DIR      日志目录
#   LOOKBACK_HOURS              回看窗口（小时），默认 2（重叠 1 小时防漏）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_ROOT="${SCHEDULER_DATAJOB_SYNC_DIR:-$SCRIPT_DIR}"
LOG_DIR="${SCHEDULER_SYNC_LOG_DIR:-$SYNC_ROOT/logs}"
LOG_FILE="$LOG_DIR/scheduler_datajob_sync_hourly_$(date +%Y%m%d_%H).log"
ENV_FILE="$SYNC_ROOT/lineage.env"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-2}"

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

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$SYNC_ROOT"
export PYTHONPATH="$SYNC_ROOT"

ACTIVITY_SINCE=$(date -d "${LOOKBACK_HOURS} hours ago" +%Y-%m-%dT%H:%M:%S)

echo "===================================================================" | tee -a "$LOG_FILE"
echo "[start] $(date -Is) scheduler DataJob hourly activity sync" | tee -a "$LOG_FILE"
echo " sync_root=$SYNC_ROOT" | tee -a "$LOG_FILE"
echo " python=$PYTHON" | tee -a "$LOG_FILE"
echo " lookback_hours=$LOOKBACK_HOURS" | tee -a "$LOG_FILE"
echo " activity_since=$ACTIVITY_SINCE" | tee -a "$LOG_FILE"
echo " log=$LOG_FILE" | tee -a "$LOG_FILE"
echo "===================================================================" | tee -a "$LOG_FILE"

"$PYTHON" -u -m scheduler_datajob_sync.sync_datajobs \
  --activity-since "$ACTIVITY_SINCE" \
  2>&1 | tee -a "$LOG_FILE"
status=${PIPESTATUS[0]}
echo "[exit] $(date -Is) status=$status log=$LOG_FILE" | tee -a "$LOG_FILE"
exit "$status"
