#!/usr/bin/env bash
# run_batch_table_lineage_sync_from_datasets.sh — 按 DataHub 表属性重跑表级血缘
#
# 优先：若表已有 LLM 生成的 Documentation，从 description 中「4. 数据来源」解析上游表
#       （与 run_check_datahub_dataset_availability.sh 相同解析逻辑）。
#       写入前逐条校验 Hive 表存在性：不存在的上游表跳过，不加入 lineage。
# 否则：读取 Etl Script / Execute Shell，调用 LLM 解析目标表与上游表。
#
# Jenkins 参数：
#   TABLE_NAMES             Multi-line：每行一个 db.table
#   CLEAR_EXISTING_LINEAGE  1=替换已有血缘（默认）；0=合并已有血缘；2=只检查不写入
#   CONCURRENCY             并发数（默认 10）
#   DRY_RUN                 1=不写 DataHub
#   LLM_TIMEOUT             LLM 超时秒数（默认 90）
#   LINEAGE_PYTHON          Python 解释器
#   DATAHUB_GMS_URL         GMS 地址
#   DATAHUB_GMS_TOKEN       GMS token
set -euo pipefail

CONCURRENCY="${CONCURRENCY:-10}"
DRY_RUN="${DRY_RUN:-0}"
LLM_TIMEOUT="${LLM_TIMEOUT:-90}"
CLEAR_EXISTING_LINEAGE="${CLEAR_EXISTING_LINEAGE:-1}"
TABLE_LIST_CLEAR="${TABLE_LIST_CLEAR:-1}"

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

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

mkdir -p "$REPORT_DIR"

_BATCH_REPORT="$REPORT_DIR/batch_report_table_list.jsonl"
_AUDIT_JSONL="$REPORT_DIR/lineage_audit_table_list.jsonl"
_EXCEL_OUT="$REPORT_DIR/lineage_report_table_list.xlsx"
_TABLES_SNAPSHOT="$REPORT_DIR/table_names_to_rerun.txt"

echo "==================================================================="
echo " Batch table lineage sync from DataHub table structured properties"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  PYTHONPATH_ROOT=$PYTHONPATH_ROOT"
echo " REPORT_DIR=$REPORT_DIR"
echo " CONCURRENCY=$CONCURRENCY  DRY_RUN=$DRY_RUN"
echo " CLEAR_EXISTING_LINEAGE=$CLEAR_EXISTING_LINEAGE"
echo " LLM_TIMEOUT=$LLM_TIMEOUT"
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

export DATAHUB_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-eyJhbGciOiJIUzI1NiJ9.eyJhY3RvclR5cGUiOiJVU0VSIiwiYWN0b3JJZCI6ImRhdGFodWIiLCJ0eXBlIjoiUEVSU09OQUwiLCJ2ZXJzaW9uIjoiMiIsImp0aSI6IjgxMDY0Zjk0LWNmOWEtNGMzZS04MDU5LTExMzc5OTU1MzM5MCIsInN1YiI6ImRhdGFodWIiLCJpc3MiOiJkYXRhaHViLW1ldGFkYXRhLXNlcnZpY2UifQ.pPRncAU5T3P2PeP78q1f53KdS56rNZpeQJ8AUMjSbrw}"

if ! "$PYTHON" -c "import openpyxl; from datahub.emitter.rest_emitter import DatahubRestEmitter; from datahub.ingestion.graph.client import DataHubGraph" 2>/dev/null; then
  echo "ERROR: 依赖 import 失败（解释器: $PYTHON，用户: $(id -un)）。" >&2
  echo "请在该环境中执行: $PYTHON -m pip install -U openpyxl 'acryl-datahub>=0.12'" >&2
  exit 1
fi
echo "[INFO] Python 依赖自检通过 ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

if [[ ! -f "$PKG_DIR/table_lineage_from_dataset_props.py" ]]; then
  echo "ERROR: $PKG_DIR/table_lineage_from_dataset_props.py 不存在。" >&2
  exit 1
fi
echo "[INFO] table_lineage_from_dataset_props.py 版本验证通过"

if [[ -z "${TABLE_NAMES:-}" ]]; then
  echo "ERROR: 请设置 Jenkins 参数 TABLE_NAMES（Multi-line，每行一个 db.table）。" >&2
  exit 2
fi
printf '%s\n' "$TABLE_NAMES" > "$_TABLES_SNAPSHOT"
echo "[INFO] 从 TABLE_NAMES 环境变量写入 $_TABLES_SNAPSHOT"

if [[ "$TABLE_LIST_CLEAR" == "1" ]]; then
  for _f in "$_BATCH_REPORT" "$_AUDIT_JSONL" "$_EXCEL_OUT"; do
    if [[ -f "$_f" ]]; then
      echo "[INFO] 清空旧报告: $_f"
      : > "$_f"
    fi
  done
  find "$PKG_DIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
fi

ARGS="--report $_BATCH_REPORT"
ARGS="$ARGS --audit-jsonl $_AUDIT_JSONL"
ARGS="$ARGS --table-file $_TABLES_SNAPSHOT"
ARGS="$ARGS --concurrency $CONCURRENCY"
ARGS="$ARGS --llm-timeout $LLM_TIMEOUT"
[[ "$DRY_RUN" == "1" ]] && ARGS="$ARGS --dry-run"
[[ "$CLEAR_EXISTING_LINEAGE" == "0" ]] && ARGS="$ARGS --merge-existing-lineage"
[[ "$CLEAR_EXISTING_LINEAGE" == "2" ]] && ARGS="$ARGS --check-existing-lineage"
[[ -n "${DATAHUB_GMS_URL:-}" ]] && ARGS="$ARGS --datahub-gms $DATAHUB_GMS_URL"
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS="$ARGS --token $DATAHUB_GMS_TOKEN"
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS="$ARGS --platform-instance $BLF_DATAHUB_PLATFORM_INSTANCE"
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS="$ARGS --env $DATAHUB_ENV"

echo "[INFO] running: $PYTHON -m job_info_sync_datahub.table_lineage_from_dataset_props $ARGS"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.table_lineage_from_dataset_props $ARGS

echo "[INFO] exporting Excel report..."
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.export_lineage_excel \
    --audit  "$_AUDIT_JSONL" \
    --report "$_BATCH_REPORT" \
    --output "$_EXCEL_OUT"

echo "[DONE] table-list reports -> $REPORT_DIR/"
echo "  batch: $_BATCH_REPORT"
echo "  audit: $_AUDIT_JSONL"
echo "  excel: $_EXCEL_OUT"
