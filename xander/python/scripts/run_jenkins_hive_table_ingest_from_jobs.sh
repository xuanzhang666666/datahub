#!/usr/bin/env bash
# run_jenkins_hive_table_ingest_from_jobs.sh — Jenkins：按 JOBS 参数将 Hive 表列表同步到 DataHub
#
# ── Jenkins 参数 ─────────────────────────────────────────────────────────────
# JOBS              Multi-line String，每行一张表：库.表（如 data_sec_dw.dim_store_info）
#                   也支持仅表名（须设 HIVE_INGEST_IMPLICIT_DATABASE，默认 default）
#
# ── 行为 ───────────────────────────────────────────────────────────────────
# 对名单中每张表：若 DataHub 中已有对应 Dataset → hard delete → 再写入 DataHub
# 不做 fqtn 层级前缀等表名校验，严格按 JOBS 列表导入
#
# ── 速度（重要）────────────────────────────────────────────────────────────
# BLF_HIVE_INGEST_MODE=minimal   轻量 MCP 注册（秒级，适合单表/补血缘节点，无列 schema）
# BLF_HIVE_INGEST_MODE=full      默认：datahub ingest；会对 HMS 整库 get_all_tables 再逐表拉元数据，
#                                default 等大库即使 JOBS 只有 1 张表也可能跑很久（非 bug）
#
# ── 常用环境变量（与批量血缘任务相同）──────────────────────────────────────
# LINEAGE_PYTHON / HIVE_INGEST_PYTHON   推荐 /opt/anaconda3/bin/python
# DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN   可写在 lineage.env
# HMS_THRIFT_HOST / HMS_THRIFT_PORT     默认 hiveserver5.dp.data.bj1.wormpex.com:9083
# HIVE_INGEST_CHUNK_SIZE                recipe 正则分块，默认 600
# BLF_HIVE_INGEST_TIMEOUT_SEC           ingest 超时，默认 1800
# HIVE_INGEST_INCLUDE_VIEW_LINEAGE=1    开启视图血缘（慢）
# DRY_RUN=1                             只打印计划，不删不写
#
# ── 可选 ───────────────────────────────────────────────────────────────────
# JOB_FILE            表名单文件（未设 JOBS 时使用）
# TABLE_LIST_FILE     JOBS 落盘路径（默认 $REPORT_DIR/hive_tables_to_ingest.txt）
# LINEAGE_ENV_FILE / lineage.env
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"

if [[ -n "${LINEAGE_REPORT_DIR:-}" ]]; then
  REPORT_DIR="$LINEAGE_REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  REPORT_DIR="$WORKSPACE/lineage_reports"
else
  REPORT_DIR="$SCRIPT_DIR/lineage_reports"
fi
mkdir -p "$REPORT_DIR"

_TABLE_SNAPSHOT="${TABLE_LIST_FILE:-$REPORT_DIR/hive_tables_to_ingest.txt}"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -n "${HIVE_INGEST_PYTHON:-}" ]]; then
  PYTHON="$HIVE_INGEST_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

echo "==================================================================="
echo " Jenkins Hive table list -> DataHub"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  REPORT_DIR=$REPORT_DIR"
echo " DRY_RUN=${DRY_RUN:-0}"
echo " BLF_HIVE_INGEST_MODE=${BLF_HIVE_INGEST_MODE:-full}"
echo "==================================================================="

for _cand in "${LINEAGE_ENV_FILE:-}" "$SCRIPT_DIR/lineage.env" ${WORKSPACE:+"$WORKSPACE/lineage.env"}; do
  [[ -z "$_cand" ]] && continue
  if [[ -r "$_cand" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$_cand"; set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

if ! "$PYTHON" -c "import datahub; from datahub.ingestion.graph.client import DataHubGraph" 2>/dev/null; then
  echo "ERROR: 需要 acryl-datahub[hive-metastore]（$PYTHON）" >&2
  exit 1
fi

if [[ ! -f "$PKG_DIR/hive_jobs_table_ingest.py" ]]; then
  echo "ERROR: 未找到 $PKG_DIR/hive_jobs_table_ingest.py，请部署 job_info_sync_datahub 包" >&2
  exit 1
fi

_RESOLVED_LIST=""
if [[ -n "${JOBS:-}" ]]; then
  printf '%s\n' "$JOBS" > "$_TABLE_SNAPSHOT"
  _RESOLVED_LIST="$_TABLE_SNAPSHOT"
  echo "[INFO] 从 JOBS 写入表名单: $_TABLE_SNAPSHOT"
elif [[ -n "${JOB_FILE:-}" ]]; then
  _RESOLVED_LIST="$JOB_FILE"
elif [[ $# -ge 1 && -f "$1" ]]; then
  _RESOLVED_LIST="$1"
else
  echo "ERROR: 请设置 Jenkins 参数 JOBS（Multi-line，每行 库.表），或 JOB_FILE / 传入名单文件。" >&2
  exit 2
fi

if [[ ! -f "$_RESOLVED_LIST" ]]; then
  echo "ERROR: 表名单文件不存在: $_RESOLVED_LIST" >&2
  exit 2
fi

ARGS=(--table-list-file "$_RESOLVED_LIST")
[[ -n "${DATAHUB_GMS_URL:-}" ]] && ARGS+=(--datahub-gms "$DATAHUB_GMS_URL")
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS+=(--token "$DATAHUB_GMS_TOKEN")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS+=(--env "$DATAHUB_ENV")
[[ -n "${HIVE_INGEST_IMPLICIT_DATABASE:-}" ]] && ARGS+=(--implicit-database "$HIVE_INGEST_IMPLICIT_DATABASE")
[[ "${DRY_RUN:-0}" == "1" ]] && ARGS+=(--dry-run)
[[ "${HIVE_INGEST_INCLUDE_VIEW_LINEAGE:-}" == "1" ]] && ARGS+=(--include-view-lineage)
[[ -n "${HIVE_INGEST_CHUNK_SIZE:-}" ]] && ARGS+=(--chunk-size "$HIVE_INGEST_CHUNK_SIZE")
[[ -n "${BLF_HIVE_INGEST_MODE:-}" ]] && ARGS+=(--ingest-mode "$BLF_HIVE_INGEST_MODE")

echo "[INFO] PYTHON=$PYTHON PYTHONPATH=$PYTHONPATH_ROOT"
echo "[INFO] $PYTHON -m job_info_sync_datahub.hive_jobs_table_ingest ${ARGS[*]}"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.hive_jobs_table_ingest "${ARGS[@]}"

echo "[DONE] hive table ingest from JOBS"
