#!/usr/bin/env bash
# run_field_lineage_import_to_datahub.sh — Jenkins / neo4j2：从审核 Excel 导入字段级血缘到 DataHub
#
# 部署路径：/data/datahub/scripts/run_field_lineage_import_to_datahub.sh
#
# ── 必填（与导出任务配对）────────────────────────────────────────────────────
# BATCH_CODE           导出任务日志中的批次号 YYYYMMDDHHmm（12 位数字）
# TABLES               Jenkins multi-line string parameter，一行一个 库.表（与导出时相同）
#
# Excel 路径规则（与 run_field_lineage_export_to_excel.sh 一致）：
#   /data/datahub/out/field_lineage_export/{BATCH_CODE}_{库.表}.xlsx
#
# ── 写入 ─────────────────────────────────────────────────────────────────────
# 默认写入 DataHub fineGrainedLineages（--write）
# DRY_RUN=1 或 FIELD_LINEAGE_DRY_RUN=1  → 仅 dry-run，不写 GMS
#
# ── 可选 ─────────────────────────────────────────────────────────────────────
# FIELD_LINEAGE_INPUT_DIR   默认 /data/datahub/out/field_lineage_export
# DATAHUB_GMS_URL           默认 http://localhost:8080
# LINEAGE_PYTHON            默认 /opt/anaconda3/bin/python
#
# Jenkins：BATCH_CODE + TABLES（Multi-line String Parameter），Execute shell 直接引用即可
#   sh /data/datahub/scripts/run_field_lineage_import_to_datahub.sh
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

INPUT_DIR="${FIELD_LINEAGE_INPUT_DIR:-/data/datahub/out/field_lineage_export}"
GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
BATCH_CODE="${BATCH_CODE:-}"
DRY_RUN_FLAG="${FIELD_LINEAGE_DRY_RUN:-${DRY_RUN:-0}}"

for _cand in "${LINEAGE_ENV_FILE:-}" "$SCRIPT_DIR/lineage.env" ${WORKSPACE:+"$WORKSPACE/lineage.env"}; do
  [[ -z "$_cand" ]] && continue
  if [[ -r "$_cand" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$_cand"
    set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

if [[ -z "${BATCH_CODE//[[:space:]]/}" ]]; then
  echo "ERROR: BATCH_CODE 为空，请填写导出任务日志中的批次号（YYYYMMDDHHmm，12 位数字）。" >&2
  exit 2
fi
BATCH_CODE="${BATCH_CODE//[[:space:]]/}"
if ! [[ "$BATCH_CODE" =~ ^[0-9]{12}$ ]]; then
  echo "ERROR: BATCH_CODE 格式无效，必须为 12 位 YYYYMMDDHHmm，当前: $BATCH_CODE" >&2
  exit 2
fi

# Jenkins multi-line string parameter：一行一个 库.表
_parse_tables_multiline() {
  TABLE_LIST=()
  local _raw="${TABLES:-}"
  if [[ -z "${_raw//[[:space:]]/}" ]]; then
    echo "ERROR: TABLES 为空，请在 Jenkins Multi-line String Parameter 中填写表名（一行一个 库.表）。" >&2
    exit 2
  fi
  _raw="${_raw//$'\r'/}"
  while IFS= read -r _line || [[ -n "$_line" ]]; do
    _line="${_line#"${_line%%[![:space:]]*}"}"
    _line="${_line%"${_line##*[![:space:]]}"}"
    [[ -z "$_line" ]] && continue
    if [[ "$_line" != *.* ]] || [[ "$_line" == .* ]] || [[ "$_line" == *. ]]; then
      echo "ERROR: 表名必须为 库.表 格式（例如 default.dim_store_info），无效行: $_line" >&2
      exit 2
    fi
    TABLE_LIST+=("$_line")
  done <<< "$_raw"
  if [[ ${#TABLE_LIST[@]} -eq 0 ]]; then
    echo "ERROR: TABLES 未解析到有效表名（一行一个 库.表）。" >&2
    exit 2
  fi
}
_parse_tables_multiline

IMPORT_WRITE=1
case "$(echo "$DRY_RUN_FLAG" | tr '[:upper:]' '[:lower:]')" in
  1 | true | yes) IMPORT_WRITE=0 ;;
  0 | false | no | "") IMPORT_WRITE=1 ;;
  *)
    echo "ERROR: DRY_RUN / FIELD_LINEAGE_DRY_RUN 无法识别: $DRY_RUN_FLAG" >&2
    exit 2
    ;;
esac

if [[ ! -d "$INPUT_DIR" ]]; then
  echo "ERROR: Excel 目录不存在: $INPUT_DIR" >&2
  exit 1
fi

echo "[INFO] field lineage import started at $(date -Iseconds)"
echo "[INFO] batch code: $BATCH_CODE"
echo "[INFO] input dir: $INPUT_DIR"
echo "[INFO] write to datahub: $([[ "$IMPORT_WRITE" -eq 1 ]] && echo yes || echo no)"
echo "[INFO] table count: ${#TABLE_LIST[@]}"
echo "[INFO] tables: ${TABLE_LIST[*]}"
echo "[INFO] gms url: $GMS_URL"
echo "[INFO] python: $PYTHON"

_run_cli() {
  PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" -m job_info_sync_datahub.field_lineage_cli "$@"
}

_fail=0
for TABLE_NAME in "${TABLE_LIST[@]}"; do
  EXCEL_FILE="$INPUT_DIR/${BATCH_CODE}_${TABLE_NAME}.xlsx"
  PLAN_FILE="$INPUT_DIR/${BATCH_CODE}_${TABLE_NAME}_import_plan.json"

  echo "[INFO] ----------------------------------------"
  echo "[INFO] table: $TABLE_NAME"
  echo "[INFO] excel: $EXCEL_FILE"

  if [[ ! -f "$EXCEL_FILE" ]]; then
    echo "[ERROR] 找不到 Excel: $EXCEL_FILE" >&2
    _fail=$((_fail + 1))
    continue
  fi

  IMPORT_ARGS=(
    import-reviewed
    --input "$EXCEL_FILE"
    --output "$PLAN_FILE"
    --gms-url "$GMS_URL"
  )
  if [[ "$IMPORT_WRITE" -eq 1 ]]; then
    IMPORT_ARGS+=(--write)
  fi

  if ! _run_cli "${IMPORT_ARGS[@]}"; then
    echo "[ERROR] import failed: $TABLE_NAME" >&2
    _fail=$((_fail + 1))
    continue
  fi
  echo "[INFO] import ok: $TABLE_NAME (plan: $PLAN_FILE)"
done

_ok=$((${#TABLE_LIST[@]} - _fail))
echo "[INFO] field lineage import finished at $(date -Iseconds)"
echo "[INFO] batch code: $BATCH_CODE"
echo "[INFO] succeeded: ${_ok}/${#TABLE_LIST[@]}"

if [[ "$_fail" -gt 0 ]]; then
  exit 1
fi
