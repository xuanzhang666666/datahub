#!/usr/bin/env bash
# run_deprecate_hive_tables.sh — Jenkins：按 TABLE_NAMES 批量标记 Hive 表废弃并写结构化属性
#
# Jenkins 参数：
#   TABLE_NAMES  Multi-line String，一行一个表名；支持 表名 / 库.表 / platform_instance.库.表
#
# 写入内容：
#   Mark as Deprecated
#   blf.data.schedule.schedule_url=无
#   blf.data.warehouse.etl_script=无
#   blf.data.warehouse.other_remark=已废弃
#   blf.data.schedule.execute_shell=无
#
# 常用环境变量：
#   DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN / DATAHUB_ACTOR
#   BLF_DATAHUB_PLATFORM_INSTANCE  默认 blf-prod-hive
#   DATAHUB_ENV                    默认 PROD
#   HIVE_IMPLICIT_DATABASE         默认 default
#   DEPRECATION_NOTE               默认 已废弃
#   DRY_RUN=1                      只打印计划，不实际写入
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

if [[ -n "${LINEAGE_REPORT_DIR:-}" ]]; then
  REPORT_DIR="$LINEAGE_REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  REPORT_DIR="$WORKSPACE/lineage_reports"
else
  REPORT_DIR="$SCRIPT_DIR/lineage_reports"
fi
mkdir -p "$REPORT_DIR"

TABLE_SNAPSHOT="${TABLE_LIST_FILE:-$REPORT_DIR/deprecate_hive_tables.txt}"
REPORT_FILE="${DEPRECATE_TABLE_REPORT:-$REPORT_DIR/deprecate_hive_tables_report.jsonl}"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
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

export DATAHUB_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-eyJhbGciOiJIUzI1NiJ9.eyJhY3RvclR5cGUiOiJVU0VSIiwiYWN0b3JJZCI6ImRhdGFodWIiLCJ0eXBlIjoiUEVSU09OQUwiLCJ2ZXJzaW9uIjoiMiIsImp0aSI6IjgxMDY0Zjk0LWNmOWEtNGMzZS04MDU5LTExMzc5OTU1MzM5MCIsInN1YiI6ImRhdGFodWIiLCJpc3MiOiJkYXRhaHViLW1ldGFkYXRhLXNlcnZpY2UifQ.pPRncAU5T3P2PeP78q1f53KdS56rNZpeQJ8AUMjSbrw}"

RESOLVED_LIST=""
if [[ -n "${TABLE_NAMES:-}" ]]; then
  printf '%s\n' "$TABLE_NAMES" > "$TABLE_SNAPSHOT"
  RESOLVED_LIST="$TABLE_SNAPSHOT"
  echo "[INFO] 从 TABLE_NAMES 写入表名单: $TABLE_SNAPSHOT"
elif [[ -n "${TABLE_FILE:-}" ]]; then
  RESOLVED_LIST="$TABLE_FILE"
elif [[ $# -ge 1 && -f "$1" ]]; then
  RESOLVED_LIST="$1"
else
  echo "ERROR: 请设置 Jenkins 参数 TABLE_NAMES（Multi-line，每行一张表），或 TABLE_FILE / 传入名单文件。" >&2
  exit 2
fi

ARGS=(--table-list-file "$RESOLVED_LIST" --report "$REPORT_FILE")
ARGS+=(--datahub-gms "${DATAHUB_GMS_URL:-http://localhost:8080}")
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS+=(--token "$DATAHUB_GMS_TOKEN")
[[ -n "${DATAHUB_ACTOR:-}" ]] && ARGS+=(--actor "$DATAHUB_ACTOR")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS+=(--env "$DATAHUB_ENV")
[[ -n "${HIVE_IMPLICIT_DATABASE:-}" ]] && ARGS+=(--implicit-database "$HIVE_IMPLICIT_DATABASE")
[[ -n "${DEPRECATION_NOTE:-}" ]] && ARGS+=(--note "$DEPRECATION_NOTE")
[[ "${DRY_RUN:-0}" == "1" ]] && ARGS+=(--dry-run)

echo "==================================================================="
echo " Batch deprecate Hive tables"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR"
echo " REPORT=$REPORT_FILE"
echo " DRY_RUN=${DRY_RUN:-0}"
echo "==================================================================="

PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.deprecate_hive_tables "${ARGS[@]}"

echo "[DONE] deprecate hive tables report -> $REPORT_FILE"
