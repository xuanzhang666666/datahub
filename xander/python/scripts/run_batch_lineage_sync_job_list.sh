#!/usr/bin/env bash
# run_batch_lineage_sync_job_list.sh — 按作业名单重跑血缘（Jenkins Multi-line JOBS 或文件）
# 与 run_batch_lineage_sync.sh 逻辑相同，但不用 PREFIX；独立报告文件；默认 --force。
set -euo pipefail

# ── 参数（环境变量）───────────────────────────────────────────────────────────
# JOBS              Jenkins Multi-line：每行一个 job_display_name（优先）
# JOB_FILE          作业名单文件路径（次优先；未设 JOBS 时可用）
# JOB_LIST_FILE     JOBS 落盘路径（默认 $REPORT_DIR/jobs_to_rerun.txt）
# JOB_LIST_REPORT   名单批次报告 jsonl（默认 $REPORT_DIR/batch_report_job_list.jsonl）
# JOB_LIST_CLEAR    1=本次运行前清空独立报告（默认 1）；0=断点续跑
# CONCURRENCY       并发数（默认 10）
# DRY_RUN           1=不写 DataHub
# LLM_TIMEOUT       LLM 超时秒数（默认 90）
# JOB_INFO_SYNC_DIR Python 包目录
# LINEAGE_REPORT_DIR 报告根目录

CONCURRENCY="${CONCURRENCY:-10}"
DRY_RUN="${DRY_RUN:-0}"
LLM_TIMEOUT="${LLM_TIMEOUT:-90}"
JOB_FILE="${JOB_FILE:-}"
JOB_LIST_CLEAR="${JOB_LIST_CLEAR:-1}"

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

_BATCH_REPORT="${JOB_LIST_REPORT:-$REPORT_DIR/batch_report_job_list.jsonl}"
_AUDIT_JSONL="$REPORT_DIR/lineage_audit_job_list.jsonl"
_EXCEL_OUT="$REPORT_DIR/lineage_report_job_list.xlsx"
_JOBS_SNAPSHOT="${JOB_LIST_FILE:-$REPORT_DIR/jobs_to_rerun.txt}"

echo "==================================================================="
echo " Batch lineage sync (job list) started"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  PYTHONPATH_ROOT=$PYTHONPATH_ROOT"
echo " REPORT_DIR=$REPORT_DIR"
echo " CONCURRENCY=$CONCURRENCY  DRY_RUN=$DRY_RUN  JOB_LIST_CLEAR=$JOB_LIST_CLEAR"
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

if ! "$PYTHON" -c "import trino, openpyxl; from datahub.emitter.rest_emitter import DatahubRestEmitter" 2>/dev/null; then
  echo "ERROR: 依赖 import 失败（解释器: $PYTHON，用户: $(id -un)）。" >&2
  if [[ "$PYTHON" == /root/* ]] && [[ "$(id -u)" -ne 0 ]]; then
    echo "Jenkins 以非 root 运行时，通常不能读取 /root 下 Anaconda；请 export LINEAGE_PYTHON=/opt/anaconda3/bin/python" >&2
  else
    echo "请在该环境中执行: $PYTHON -m pip install -U trino openpyxl 'acryl-datahub>=0.12'" >&2
  fi
  exit 1
fi
echo "[INFO] Python 依赖自检通过 ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

if [[ ! -f "$PKG_DIR/batch_sync.py" ]]; then
  echo "ERROR: $PKG_DIR/batch_sync.py 不存在。" >&2
  echo "  请在服务器上部署: cd $SCRIPT_DIR && get2 job_info_sync_datahub.tar.gz && tar xzf job_info_sync_datahub.tar.gz" >&2
  exit 1
fi
if ! grep -q 'audit-jsonl' "$PKG_DIR/batch_sync.py"; then
  echo "ERROR: batch_sync.py 版本过旧（缺少 --audit-jsonl），请重新打包上传 FTP" >&2
  exit 1
fi
if ! grep -q '\-\-force' "$PKG_DIR/batch_sync.py"; then
  echo "ERROR: batch_sync.py 版本过旧（缺少 --force），请重新打包上传 FTP" >&2
  exit 1
fi
echo "[INFO] batch_sync.py 版本验证通过"

# ── 解析作业名单 ───────────────────────────────────────────────────────────────
_RESOLVED_JOB_FILE=""
if [[ -n "${JOBS:-}" ]]; then
  printf '%s\n' "$JOBS" > "$_JOBS_SNAPSHOT"
  _RESOLVED_JOB_FILE="$_JOBS_SNAPSHOT"
  echo "[INFO] 从 JOBS 环境变量写入 $_JOBS_SNAPSHOT"
elif [[ -n "$JOB_FILE" ]]; then
  _RESOLVED_JOB_FILE="$JOB_FILE"
elif [[ $# -ge 1 && -f "$1" ]]; then
  _RESOLVED_JOB_FILE="$1"
else
  echo "ERROR: 请设置 Jenkins 参数 JOBS（Multi-line，每行一个 job_display_name），或 JOB_FILE，或传入名单文件路径。" >&2
  exit 1
fi

if [[ ! -f "$_RESOLVED_JOB_FILE" ]]; then
  echo "ERROR: 作业名单文件不存在: $_RESOLVED_JOB_FILE" >&2
  exit 1
fi

# ── 仅清空名单重跑专用报告（不影响全量 batch_report.jsonl）────────────────────
if [[ "$JOB_LIST_CLEAR" == "1" ]]; then
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
ARGS="$ARGS --job-file $_RESOLVED_JOB_FILE"
ARGS="$ARGS --force"
ARGS="$ARGS --concurrency $CONCURRENCY"
ARGS="$ARGS --llm-timeout $LLM_TIMEOUT"
[[ "$DRY_RUN" == "1" ]] && ARGS="$ARGS --dry-run"

echo "[INFO] running: $PYTHON -m job_info_sync_datahub.batch_sync $ARGS"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.batch_sync $ARGS

echo "[INFO] exporting Excel report..."
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.export_lineage_excel \
    --audit  "$_AUDIT_JSONL" \
    --report "$_BATCH_REPORT" \
    --output "$_EXCEL_OUT"

echo "[DONE] job-list reports -> $REPORT_DIR/"
echo "  batch: $_BATCH_REPORT"
echo "  audit: $_AUDIT_JSONL"
echo "  excel: $_EXCEL_OUT"
