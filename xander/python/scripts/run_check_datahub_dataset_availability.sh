#!/usr/bin/env bash
# run_check_datahub_dataset_availability.sh — 核对 DataHub Hive dataset 可用性并更新 data_availability_flag
#
# Jenkins 参数：
#   TABLE_NAMES       Multi-line：每行一个 db.table；为空时可用 TABLE_PRE 自动发现
#   TABLE_PRE         表名前缀过滤，如 pdw → 遍历 *.pdw* datasets（仅 TABLE_NAMES 为空时生效）
#   SET_AVAILABLE_FLAGS 1=仅遍历 TABLE_NAMES，将 Data Availability Flag 设置为 ["DDL", "表血缘"]
#   DRY_RUN           1=只生成报告和日志，不写 DataHub（默认）；0=写入 data_availability_flag
#   DATAHUB_GMS_URL   GMS 地址，默认 http://localhost:8080
#   DATAHUB_GMS_TOKEN GMS token（无鉴权时可不填）
#   LINEAGE_PYTHON    Python 解释器，默认 /opt/anaconda3/bin/python
#   REPORT_DIR        报告目录，默认 $WORKSPACE/data_availability_reports
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
    set -a
    # shellcheck disable=SC1090
    source "$_cand"
    set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

TABLE_NAMES="${TABLE_NAMES:-}"
TABLE_PRE="${TABLE_PRE:-}"
SET_AVAILABLE_FLAGS="${SET_AVAILABLE_FLAGS:-0}"
if [[ "$SET_AVAILABLE_FLAGS" == "1" && -z "${TABLE_NAMES//[[:space:]]/}" ]]; then
  echo "ERROR: SET_AVAILABLE_FLAGS=1 时必须填写 TABLE_NAMES。" >&2
  exit 2
fi
if [[ -z "${TABLE_NAMES//[[:space:]]/}" && -z "${TABLE_PRE//[[:space:]]/}" ]]; then
  echo "ERROR: TABLE_NAMES 和 TABLE_PRE 都为空，请填写表名或表名前缀。" >&2
  exit 2
fi

GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
DRY_RUN="${DRY_RUN:-1}"
REPORT_DIR="${REPORT_DIR:-${WORKSPACE:-$(pwd)}/data_availability_reports}"
TABLE_FILE="$REPORT_DIR/table_names_to_check.txt"
JSONL_PATH="$REPORT_DIR/data_availability_report.jsonl"
XLSX_PATH="$REPORT_DIR/data_availability_report.xlsx"

mkdir -p "$REPORT_DIR"
: > "$JSONL_PATH"

ARGS=(
  --datahub-gms "$GMS_URL"
  --jsonl "$JSONL_PATH"
  --xlsx "$XLSX_PATH"
)
if [[ -n "${TABLE_NAMES//[[:space:]]/}" ]]; then
  printf '%s\n' "$TABLE_NAMES" > "$TABLE_FILE"
  ARGS+=(--table-list-file "$TABLE_FILE")
elif [[ -n "${TABLE_PRE//[[:space:]]/}" ]]; then
  : > "$TABLE_FILE"
  ARGS+=(--table-prefix "$TABLE_PRE")
fi
if [[ "$DRY_RUN" == "1" ]]; then
  ARGS+=(--dry-run)
fi
if [[ "$SET_AVAILABLE_FLAGS" == "1" ]]; then
  ARGS+=(--set-available-flags)
fi
if [[ -n "${DATAHUB_GMS_TOKEN:-}" ]]; then
  ARGS+=(--token "$DATAHUB_GMS_TOKEN")
fi
if [[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]]; then
  ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
fi
if [[ -n "${DATAHUB_ENV:-}" ]]; then
  ARGS+=(--env "$DATAHUB_ENV")
fi

echo "==================================================================="
echo " DataHub dataset availability check"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  PYTHONPATH_ROOT=$PYTHONPATH_ROOT"
echo " REPORT_DIR=$REPORT_DIR"
echo " DATAHUB_GMS_URL=$GMS_URL"
echo " DRY_RUN=$DRY_RUN"
echo " TABLE_PRE=${TABLE_PRE:-}"
echo " SET_AVAILABLE_FLAGS=$SET_AVAILABLE_FLAGS"
echo "==================================================================="

cd "$PYTHONPATH_ROOT"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.check_dataset_availability "${ARGS[@]}"

echo "[DONE] reports:"
echo "  jsonl: $JSONL_PATH"
echo "  xlsx:  $XLSX_PATH"
