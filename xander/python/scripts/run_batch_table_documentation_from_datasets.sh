#!/usr/bin/env bash
# run_batch_table_documentation_from_datasets.sh — 按 DataHub 表结构化属性生成 Documentation
#
# Jenkins 参数：
#   TABLE_NAMES       Multi-line：每行一个 db.table
#   DOC_WRITE_ACTION  append=保留人工文档并追加/替换自动区块（默认）；overwrite=整体覆盖
#   DRY_RUN           1=只生成报告和 Markdown 预览，不写 DataHub（默认）；0=写入 DataHub
#   CONCURRENCY       并发数（默认 5）
#   LLM_TIMEOUT       LLM 超时秒数（默认 300）
#   MAX_CONSECUTIVE_LLM_FAILURES  连续多少个 LLM_ERROR 后停止（默认 3；<=0 不启用）
#   LINEAGE_PYTHON    Python 解释器
#   DATAHUB_GMS_URL   GMS 地址
#   DATAHUB_GMS_TOKEN GMS token
#   TRINO_*           查询 SHOW CREATE TABLE 使用
set -euo pipefail

CONCURRENCY="${CONCURRENCY:-5}"
DRY_RUN="${DRY_RUN:-1}"
LLM_TIMEOUT="${LLM_TIMEOUT:-300}"
MAX_CONSECUTIVE_LLM_FAILURES="${MAX_CONSECUTIVE_LLM_FAILURES:-3}"
DOC_WRITE_ACTION="${DOC_WRITE_ACTION:-append}"
TABLE_LIST_CLEAR="${TABLE_LIST_CLEAR:-1}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"
if [[ -n "${DOCUMENTATION_REPORT_DIR:-}" ]]; then
  REPORT_DIR="$DOCUMENTATION_REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  REPORT_DIR="$WORKSPACE/documentation_reports"
else
  REPORT_DIR="$SCRIPT_DIR/documentation_reports"
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

_REPORT="$REPORT_DIR/table_documentation_report.jsonl"
_TABLES_SNAPSHOT="$REPORT_DIR/table_names_to_document.txt"

echo "==================================================================="
echo " Batch table documentation from DataHub table structured properties"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  PYTHONPATH_ROOT=$PYTHONPATH_ROOT"
echo " REPORT_DIR=$REPORT_DIR"
echo " CONCURRENCY=$CONCURRENCY  DRY_RUN=$DRY_RUN"
echo " DOC_WRITE_ACTION=$DOC_WRITE_ACTION"
echo " LLM_TIMEOUT=$LLM_TIMEOUT"
echo " MAX_CONSECUTIVE_LLM_FAILURES=$MAX_CONSECUTIVE_LLM_FAILURES"
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

if [[ "$DOC_WRITE_ACTION" != "append" && "$DOC_WRITE_ACTION" != "overwrite" ]]; then
  echo "ERROR: DOC_WRITE_ACTION 只能为 append 或 overwrite，当前为 $DOC_WRITE_ACTION" >&2
  exit 2
fi

if [[ -z "${TABLE_NAMES:-}" ]]; then
  echo "ERROR: 请设置 Jenkins 参数 TABLE_NAMES（Multi-line，每行一个 db.table）。" >&2
  exit 2
fi
printf '%s\n' "$TABLE_NAMES" > "$_TABLES_SNAPSHOT"
echo "[INFO] 从 TABLE_NAMES 环境变量写入 $_TABLES_SNAPSHOT"

if ! "$PYTHON" -c "from datahub.emitter.rest_emitter import DatahubRestEmitter; from datahub.ingestion.graph.client import DataHubGraph" 2>/dev/null; then
  echo "ERROR: 依赖 import 失败（解释器: $PYTHON，用户: $(id -un)）。" >&2
  echo "请在该环境中执行: $PYTHON -m pip install -U 'acryl-datahub>=0.12'" >&2
  exit 1
fi
echo "[INFO] Python 依赖自检通过 ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

if ! "$PYTHON" -c "import trino" 2>/dev/null; then
  echo "[WARN] trino 依赖不可用，DDL 查询会失败但脚本会继续用 Etl Script / Execute Shell 生成文档。" >&2
fi

if [[ ! -f "$PKG_DIR/table_documentation_from_dataset_props.py" ]]; then
  echo "ERROR: $PKG_DIR/table_documentation_from_dataset_props.py 不存在。" >&2
  exit 1
fi
echo "[INFO] table_documentation_from_dataset_props.py 版本验证通过"

if [[ "$TABLE_LIST_CLEAR" == "1" ]]; then
  if [[ -f "$_REPORT" ]]; then
    echo "[INFO] 清空旧报告: $_REPORT"
    : > "$_REPORT"
  fi
  find "$PKG_DIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
fi

ARGS="--report $_REPORT"
ARGS="$ARGS --table-file $_TABLES_SNAPSHOT"
ARGS="$ARGS --concurrency $CONCURRENCY"
ARGS="$ARGS --llm-timeout $LLM_TIMEOUT"
ARGS="$ARGS --max-consecutive-llm-failures $MAX_CONSECUTIVE_LLM_FAILURES"
ARGS="$ARGS --action $DOC_WRITE_ACTION"
[[ "$DRY_RUN" == "1" ]] && ARGS="$ARGS --dry-run"
[[ -n "${DATAHUB_GMS_URL:-}" ]] && ARGS="$ARGS --datahub-gms $DATAHUB_GMS_URL"
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS="$ARGS --token $DATAHUB_GMS_TOKEN"
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS="$ARGS --platform-instance $BLF_DATAHUB_PLATFORM_INSTANCE"
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS="$ARGS --env $DATAHUB_ENV"

echo "[INFO] running: $PYTHON -m job_info_sync_datahub.table_documentation_from_dataset_props $ARGS"
cd "$PYTHONPATH_ROOT"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.table_documentation_from_dataset_props $ARGS

echo "[DONE] documentation reports -> $REPORT_DIR/"
echo "  report: $_REPORT"
echo "  markdown: $REPORT_DIR/markdown/"
echo "  ddl: $REPORT_DIR/ddl/"
echo "  llm_raw: $REPORT_DIR/llm_raw/"
