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
#   /data/datahub/out/field_lineage_export/{BATCH_CODE}/{BATCH_CODE}_{库.表}.xlsx
#   兼容旧版平铺路径：.../field_lineage_export/{BATCH_CODE}_{库.表}.xlsx
#
# ── 写入 ─────────────────────────────────────────────────────────────────────
# 默认写入 DataHub fineGrainedLineages（--write）
# DRY_RUN=1 或 FIELD_LINEAGE_DRY_RUN=1  → 仅 dry-run，不写 GMS
#
# ── 可选 ─────────────────────────────────────────────────────────────────────
# FIELD_LINEAGE_INPUT_DIR   默认 /data/datahub/out/field_lineage_export
# DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN
# LINEAGE_PYTHON            默认 /opt/anaconda3/bin/python
# FIELD_LINEAGE_CLEAR_EXISTING 默认 1；1=清空导入，0=合并更新（替换本次字段，保留其它旧字段）
# FIELD_LINEAGE_IMPORT_STATUSES 默认 AUTO_APPROVED,APPROVED；可设为 APPROVED 只导入人工审核行
# FIELD_LINEAGE_REQUIRE_FULL_AUTO_APPROVED 默认 1；1=仅当 AUTO_APPROVED=100% 且 unresolved=0 才自动导入
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
BATCH_CODE="${BATCH_CODE:-}"
DRY_RUN_FLAG="${FIELD_LINEAGE_DRY_RUN:-${DRY_RUN:-0}}"
CLEAR_EXISTING_FLAG="${FIELD_LINEAGE_CLEAR_EXISTING:-1}"
IMPORT_STATUSES="${FIELD_LINEAGE_IMPORT_STATUSES:-AUTO_APPROVED,APPROVED}"
REQUIRE_FULL_AUTO_APPROVED_FLAG="${FIELD_LINEAGE_REQUIRE_FULL_AUTO_APPROVED:-1}"

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

export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
export DATAHUB_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-eyJhbGciOiJIUzI1NiJ9.eyJhY3RvclR5cGUiOiJVU0VSIiwiYWN0b3JJZCI6ImRhdGFodWIiLCJ0eXBlIjoiUEVSU09OQUwiLCJ2ZXJzaW9uIjoiMiIsImp0aSI6IjgxMDY0Zjk0LWNmOWEtNGMzZS04MDU5LTExMzc5OTU1MzM5MCIsInN1YiI6ImRhdGFodWIiLCJpc3MiOiJkYXRhaHViLW1ldGFkYXRhLXNlcnZpY2UifQ.pPRncAU5T3P2PeP78q1f53KdS56rNZpeQJ8AUMjSbrw}"

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

IMPORT_CLEAR_EXISTING=1
case "$(echo "$CLEAR_EXISTING_FLAG" | tr '[:upper:]' '[:lower:]')" in
  1 | true | yes) IMPORT_CLEAR_EXISTING=1 ;;
  0 | false | no) IMPORT_CLEAR_EXISTING=0 ;;
  *)
    echo "ERROR: FIELD_LINEAGE_CLEAR_EXISTING 无法识别: $CLEAR_EXISTING_FLAG" >&2
    exit 2
    ;;
esac

IMPORT_REQUIRE_FULL_AUTO_APPROVED=1
case "$(echo "$REQUIRE_FULL_AUTO_APPROVED_FLAG" | tr '[:upper:]' '[:lower:]')" in
  1 | true | yes) IMPORT_REQUIRE_FULL_AUTO_APPROVED=1 ;;
  0 | false | no) IMPORT_REQUIRE_FULL_AUTO_APPROVED=0 ;;
  *)
    echo "ERROR: FIELD_LINEAGE_REQUIRE_FULL_AUTO_APPROVED 无法识别: $REQUIRE_FULL_AUTO_APPROVED_FLAG" >&2
    exit 2
    ;;
esac

if [[ ! -d "$INPUT_DIR" ]]; then
  echo "ERROR: Excel 目录不存在: $INPUT_DIR" >&2
  exit 1
fi

BATCH_INPUT_DIR="$INPUT_DIR/$BATCH_CODE"

echo "[INFO] field lineage import started at $(date -Iseconds)"
echo "[INFO] batch code: $BATCH_CODE"
echo "[INFO] input root: $INPUT_DIR"
echo "[INFO] batch input dir: $BATCH_INPUT_DIR"
echo "[INFO] write to datahub: $([[ "$IMPORT_WRITE" -eq 1 ]] && echo yes || echo no)"
echo "[INFO] import mode: $([[ "$IMPORT_CLEAR_EXISTING" -eq 1 ]] && echo clear_import || echo merge_update)"
echo "[INFO] import statuses: $IMPORT_STATUSES"
echo "[INFO] require full auto approved: $([[ "$IMPORT_REQUIRE_FULL_AUTO_APPROVED" -eq 1 ]] && echo yes || echo no)"
echo "[INFO] table count: ${#TABLE_LIST[@]}"
echo "[INFO] tables: ${TABLE_LIST[*]}"
echo "[INFO] gms url: $DATAHUB_GMS_URL"
echo "[INFO] gms token: $([[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && echo set || echo empty)"
echo "[INFO] python: $PYTHON"

_run_cli() {
  PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" -m job_info_sync_datahub.field_lineage_cli "$@"
}

_resolve_excel_file() {
  local _table="$1"
  local _name="${BATCH_CODE}_${_table}.xlsx"
  if [[ -f "$BATCH_INPUT_DIR/$_name" ]]; then
    echo "$BATCH_INPUT_DIR/$_name"
    return 0
  fi
  if [[ -f "$INPUT_DIR/$_name" ]]; then
    echo "$INPUT_DIR/$_name"
    return 0
  fi
  echo "$BATCH_INPUT_DIR/$_name"
  return 1
}

_fail=0
_skip=0
_skipped_tables=()
for TABLE_NAME in "${TABLE_LIST[@]}"; do
  if ! EXCEL_FILE="$(_resolve_excel_file "$TABLE_NAME")"; then
    EXCEL_FILE="$BATCH_INPUT_DIR/${BATCH_CODE}_${TABLE_NAME}.xlsx"
  fi
  _excel_dir="$(dirname "$EXCEL_FILE")"
  PLAN_FILE="$_excel_dir/${BATCH_CODE}_${TABLE_NAME}_import_plan.json"

  echo "[INFO] ----------------------------------------"
  echo "[INFO] table: $TABLE_NAME"
  echo "[INFO] excel: $EXCEL_FILE"

  if [[ ! -f "$EXCEL_FILE" ]]; then
    echo "[ERROR] 找不到 Excel: $EXCEL_FILE（亦已检查 $INPUT_DIR/${BATCH_CODE}_${TABLE_NAME}.xlsx）" >&2
    _fail=$((_fail + 1))
    continue
  fi

  IMPORT_ARGS=(
    import-reviewed
    --input "$EXCEL_FILE"
    --output "$PLAN_FILE"
    --gms-url "$DATAHUB_GMS_URL"
    --import-statuses "$IMPORT_STATUSES"
  )
  if [[ "$IMPORT_WRITE" -eq 1 ]]; then
    IMPORT_ARGS+=(--write)
  fi
  if [[ "$IMPORT_CLEAR_EXISTING" -eq 0 ]]; then
    IMPORT_ARGS+=(--no-clear-existing)
  fi
  if [[ "$IMPORT_REQUIRE_FULL_AUTO_APPROVED" -eq 1 ]]; then
    IMPORT_ARGS+=(--require-full-auto-approved)
  fi

  set +e
  _run_cli "${IMPORT_ARGS[@]}"
  _cli_rc=$?
  set -e
  if [[ "$_cli_rc" -eq 6 ]]; then
    echo "[WARN] import skipped: $TABLE_NAME（auto_approved_percent 不是 100% 或 unresolved_field_count 不为 0，请人工审核）" >&2
    _skip=$((_skip + 1))
    _skipped_tables+=("$TABLE_NAME")
    continue
  fi
  if [[ "$_cli_rc" -ne 0 ]]; then
    echo "[ERROR] import failed: $TABLE_NAME（若为 exit 4：Excel 无可导入行，请审核后填写 review_status=APPROVED）" >&2
    _fail=$((_fail + 1))
    continue
  fi
  if [[ "$IMPORT_WRITE" -eq 1 && -f "$PLAN_FILE" ]]; then
    _approved_count="$(PYTHONPATH="$PYTHONPATH_ROOT" "$PYTHON" -c "import json; print(json.load(open('$PLAN_FILE'))['approved_rows'])" 2>/dev/null || echo 0)"
    if [[ "${_approved_count:-0}" -eq 0 ]]; then
      echo "[ERROR] $TABLE_NAME: approved_rows=0，未写入 DataHub；请确认 Excel 中需导入行的 review_status 属于 $IMPORT_STATUSES" >&2
      _fail=$((_fail + 1))
      continue
    fi
    echo "[INFO] import ok: $TABLE_NAME (approved_rows=$_approved_count, plan: $PLAN_FILE)"
  else
    echo "[INFO] import ok: $TABLE_NAME (plan: $PLAN_FILE)"
  fi
done

_ok=$((${#TABLE_LIST[@]} - _fail - _skip))
echo "[INFO] field lineage import finished at $(date -Iseconds)"
echo "[INFO] batch code: $BATCH_CODE"
echo "[INFO] succeeded: ${_ok}/${#TABLE_LIST[@]}, skipped: ${_skip}, failed: ${_fail}"

if [[ "$_skip" -gt 0 ]]; then
  echo "[WARN] ========== 以下表未满足自动导入条件，需人工审核 =========="
  for _entry in "${_skipped_tables[@]}"; do
    echo "[WARN]   ${_entry}"
  done
  echo "[WARN] 自动导入条件：auto_approved_percent=100% 且 unresolved_field_count=0。"
fi

if [[ "$_fail" -gt 0 ]]; then
  exit 1
fi
