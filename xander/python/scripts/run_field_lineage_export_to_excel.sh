#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

# run_field_lineage_export_to_excel.sh — Jenkins / neo4j2：按表列表批量导出字段级血缘 Excel
#
# 部署路径：/data/datahub/scripts/run_field_lineage_export_to_excel.sh
#
# ── 必填 ─────────────────────────────────────────────────────────────────────
# TABLES               Jenkins multi-line string parameter，一行一个 库.表；为空则退出
#
# ── 输出 ─────────────────────────────────────────────────────────────────────
# 默认根目录：/data/datahub/out/field_lineage_export
# 每次运行写入子目录：{根目录}/{批次号}/（不存在则自动创建）
# 文件命名：{批次号}_{库.表}.xlsx，例如 .../202605181331/202605181331_default.dim_store_info.xlsx
# 批次汇总：{批次号}_field_lineage_batch_summary.xlsx（汇总已成功导出的表；部分失败退出前也会生成）
# Debug 中间产物默认写入 Jenkins WORKSPACE：
#   $WORKSPACE/field_lineage_debug/{批次号}/{库.表}/
# 未设置 WORKSPACE 时使用 CLI 默认值：Excel 同目录的 *_debug/
#   批次号每次运行自动生成 YYYYMMDDHHmm，仅打印在日志中
#
# ── 并发与重试 ───────────────────────────────────────────────────────────────
# FIELD_LINEAGE_CONCURRENCY  并发请求 LLM 数，默认 10
# FIELD_LINEAGE_LLM_BATCH_SIZE
#                            单次 LLM 解析的非分区目标字段数，默认 40；每批仍发送完整 ETL
# FIELD_LINEAGE_LLM_BATCH_CONCURRENCY
#                            单张表内部同时请求的字段批次数，默认 4
# FIELD_LINEAGE_RETRY_COUNT    失败后额外重试次数，默认 1（共最多 2 次）
# FIELD_LINEAGE_AUTO_IMPORT    导出 Excel 后自动导入 AUTO_APPROVED 字段血缘，默认 1
# FIELD_LINEAGE_AUTO_IMPORT_CLEAR_EXISTING
#                            自动导入时是否清空已有 fineGrainedLineages，默认 0（合并更新）
# FIELD_LINEAGE_AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED
#                            自动导入前是否要求整表 100% AUTO_APPROVED 且无 unresolved，默认 0
#
# ── 可选 ─────────────────────────────────────────────────────────────────────
# DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN / LINEAGE_PYTHON / FIELD_LINEAGE_PREVIEW_CHARS / FIELD_LINEAGE_LLM_TIMEOUT_SEC
# LLM_PROVIDER / LLM_MODEL   LLM 切换参数，兼容 run_batch_lineage_sync_job_list.sh
#
# Jenkins：TABLES 使用 Multi-line String Parameter，Execute shell 直接引用 $TABLES 即可
#   sh /data/datahub/scripts/run_field_lineage_export_to_excel.sh
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

OUTPUT_DIR="${FIELD_LINEAGE_OUTPUT_DIR:-/data/datahub/out/field_lineage_export}"
BATCH_ID="$(date +%Y%m%d%H%M)"
DEBUG_ROOT="${FIELD_LINEAGE_DEBUG_DIR:-${WORKSPACE:+$WORKSPACE/field_lineage_debug}}"
PREVIEW_CHARS="${FIELD_LINEAGE_PREVIEW_CHARS:-8000}"
LLM_TIMEOUT="${FIELD_LINEAGE_LLM_TIMEOUT_SEC:-240}"
CONCURRENCY="${FIELD_LINEAGE_CONCURRENCY:-10}"
export FIELD_LINEAGE_LLM_BATCH_SIZE="${FIELD_LINEAGE_LLM_BATCH_SIZE:-40}"
export FIELD_LINEAGE_LLM_BATCH_CONCURRENCY="${FIELD_LINEAGE_LLM_BATCH_CONCURRENCY:-4}"
RETRY_COUNT="${FIELD_LINEAGE_RETRY_COUNT:-1}"
AUTO_IMPORT="${FIELD_LINEAGE_AUTO_IMPORT:-1}"
AUTO_IMPORT_CLEAR_EXISTING="${FIELD_LINEAGE_AUTO_IMPORT_CLEAR_EXISTING:-0}"
AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED="${FIELD_LINEAGE_AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED:-0}"

if ! [[ "$CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: FIELD_LINEAGE_CONCURRENCY 必须为正整数: $CONCURRENCY" >&2
  exit 2
fi
if ! [[ "$FIELD_LINEAGE_LLM_BATCH_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: FIELD_LINEAGE_LLM_BATCH_SIZE 必须为正整数: $FIELD_LINEAGE_LLM_BATCH_SIZE" >&2
  exit 2
fi
if ! [[ "$FIELD_LINEAGE_LLM_BATCH_CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: FIELD_LINEAGE_LLM_BATCH_CONCURRENCY 必须为正整数: $FIELD_LINEAGE_LLM_BATCH_CONCURRENCY" >&2
  exit 2
fi
if ! [[ "$RETRY_COUNT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: FIELD_LINEAGE_RETRY_COUNT 必须为非负整数: $RETRY_COUNT" >&2
  exit 2
fi
case "$AUTO_IMPORT" in
  1 | true | TRUE | yes | YES) AUTO_IMPORT=1 ;;
  0 | false | FALSE | no | NO) AUTO_IMPORT=0 ;;
  *)
    echo "ERROR: FIELD_LINEAGE_AUTO_IMPORT 必须为 1/0/true/false: $AUTO_IMPORT" >&2
    exit 2
    ;;
esac
case "$AUTO_IMPORT_CLEAR_EXISTING" in
  1 | true | TRUE | yes | YES) AUTO_IMPORT_CLEAR_EXISTING=1 ;;
  0 | false | FALSE | no | NO) AUTO_IMPORT_CLEAR_EXISTING=0 ;;
  *)
    echo "ERROR: FIELD_LINEAGE_AUTO_IMPORT_CLEAR_EXISTING 必须为 1/0/true/false: $AUTO_IMPORT_CLEAR_EXISTING" >&2
    exit 2
    ;;
esac
case "$AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED" in
  1 | true | TRUE | yes | YES) AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED=1 ;;
  0 | false | FALSE | no | NO) AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED=0 ;;
  *)
    echo "ERROR: FIELD_LINEAGE_AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED 必须为 1/0/true/false: $AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED" >&2
    exit 2
    ;;
esac

for _cand in "${LINEAGE_ENV_FILE:-}" "$SCRIPT_DIR/lineage.env" ${WORKSPACE:+"$WORKSPACE/lineage.env"}; do
  [[ -z "$_cand" ]] && continue
  if [[ -r "$_cand" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$_cand"
    set +a
    echo "[INFO] loaded env: $_cand"
    break
  elif [[ -e "$_cand" ]]; then
    if command -v stat >/dev/null 2>&1; then
      echo "[WARN] env file exists but is not readable: $_cand ($(stat -c '%a %U %G' "$_cand" 2>/dev/null || stat -f '%Lp %Su %Sg' "$_cand" 2>/dev/null || echo 'permission unknown'))" >&2
    else
      echo "[WARN] env file exists but is not readable: $_cand" >&2
    fi
  fi
done

_load_script_env_for_missing_llm_key() {
  local _provider="$1"
  local _env_file="$SCRIPT_DIR/lineage.env"
  [[ -r "$_env_file" ]] || return 0
  case "$_provider" in
    blf | token-pool | token_pool)
      [[ -n "${BLF_LLM_API_KEY:-}" ]] && return 0
      ;;
    deepseek)
      [[ -n "${DEEPSEEK_API_KEY:-}" ]] && return 0
      ;;
    openrouter)
      [[ -n "${OPENROUTER_API_KEY:-}" ]] && return 0
      ;;
    *)
      return 0
      ;;
  esac
  set -a
  # shellcheck disable=SC1090
  source "$_env_file"
  set +a
  echo "[INFO] loaded fallback env for LLM key: $_env_file"
}

export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
export DATAHUB_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-eyJhbGciOiJIUzI1NiJ9.eyJhY3RvclR5cGUiOiJVU0VSIiwiYWN0b3JJZCI6ImRhdGFodWIiLCJ0eXBlIjoiUEVSU09OQUwiLCJ2ZXJzaW9uIjoiMiIsImp0aSI6IjgxMDY0Zjk0LWNmOWEtNGMzZS04MDU5LTExMzc5OTU1MzM5MCIsInN1YiI6ImRhdGFodWIiLCJpc3MiOiJkYXRhaHViLW1ldGFkYXRhLXNlcnZpY2UifQ.pPRncAU5T3P2PeP78q1f53KdS56rNZpeQJ8AUMjSbrw}"
LLM_PROVIDER="${LLM_PROVIDER:-${BLF_ACTIVE_LLM:-deepseek}}"
LLM_MODEL="${LLM_MODEL:-}"
_load_script_env_for_missing_llm_key "$LLM_PROVIDER"

case "$LLM_PROVIDER" in
  blf | token-pool | token_pool)
    if [[ -z "${BLF_LLM_API_KEY:-}" ]]; then
      echo "ERROR: LLM_PROVIDER=$LLM_PROVIDER 但 BLF_LLM_API_KEY 为空；请在 Jenkins 凭据、LINEAGE_ENV_FILE 或 $SCRIPT_DIR/lineage.env 中配置。" >&2
      exit 2
    fi
    ;;
  deepseek)
    if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
      echo "ERROR: LLM_PROVIDER=deepseek 但 DEEPSEEK_API_KEY 为空；请在 Jenkins 凭据、LINEAGE_ENV_FILE 或 $SCRIPT_DIR/lineage.env 中配置。" >&2
      exit 2
    fi
    ;;
  openrouter)
    if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
      echo "ERROR: LLM_PROVIDER=openrouter 但 OPENROUTER_API_KEY 为空；请在 Jenkins 凭据、LINEAGE_ENV_FILE 或 $SCRIPT_DIR/lineage.env 中配置。" >&2
      exit 2
    fi
    ;;
esac

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

BATCH_OUTPUT_DIR="$OUTPUT_DIR/$BATCH_ID"
mkdir -p "$BATCH_OUTPUT_DIR"
STATUS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/field_lineage_export.XXXXXX")"
_BATCH_SUMMARY_DONE=0
_WAIT_COMPLETED=0
_FD7_OPEN=0
_pids=()

_has_batch_export_workbooks() {
  find "$BATCH_OUTPUT_DIR" -maxdepth 1 -type f -name '*.xlsx' 2>/dev/null \
    | grep -v '_summary\.xlsx$' \
    | grep -q .
}

_write_batch_summary_once() {
  [[ "$_BATCH_SUMMARY_DONE" -eq 1 ]] && return 0
  [[ -z "${BATCH_OUTPUT_DIR:-}" || ! -d "$BATCH_OUTPUT_DIR" ]] && return 0
  if ! _has_batch_export_workbooks; then
    return 0
  fi
  local _summary_file="$BATCH_OUTPUT_DIR/${BATCH_ID}_field_lineage_batch_summary.xlsx"
  set +e
  PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" -m job_info_sync_datahub.field_lineage_batch_summary \
    --batch-dir "$BATCH_OUTPUT_DIR" \
    --output "$_summary_file"
  local _summary_rc=$?
  set -e
  if [[ "$_summary_rc" -eq 0 ]]; then
    _BATCH_SUMMARY_DONE=1
    echo "[INFO] batch summary: $_summary_file"
    return 0
  fi
  echo "[WARN] batch summary generation failed: $_summary_file" >&2
  return 1
}

_on_script_exit() {
  local _exit_code=$?
  trap - EXIT
  if [[ "$_WAIT_COMPLETED" -ne 1 && ${#_pids[@]} -gt 0 ]]; then
    echo "[WARN] export script exiting before all workers finished; cleaning child processes ..." >&2
    for _pid in "${_pids[@]}"; do
      if kill -0 "$_pid" 2>/dev/null; then
        pkill -TERM -P "$_pid" 2>/dev/null || true
        kill -TERM "$_pid" 2>/dev/null || true
      fi
    done
    sleep 1
    for _pid in "${_pids[@]}"; do
      if kill -0 "$_pid" 2>/dev/null; then
        pkill -KILL -P "$_pid" 2>/dev/null || true
        kill -KILL "$_pid" 2>/dev/null || true
      fi
      wait "$_pid" 2>/dev/null || true
    done
  fi
  if [[ "$_FD7_OPEN" -eq 1 ]]; then
    exec 7<&- 2>/dev/null || true
    exec 7>&- 2>/dev/null || true
    _FD7_OPEN=0
  fi
  _write_batch_summary_once || true
  if [[ -n "${STATUS_DIR:-}" ]]; then
    rm -rf "$STATUS_DIR"
  fi
  exit "$_exit_code"
}
# shellcheck disable=SC2064
trap _on_script_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "[INFO] field lineage export started at $(date -Iseconds)"
echo "[INFO] batch id (auto): $BATCH_ID"
echo "[INFO] output root: $OUTPUT_DIR"
echo "[INFO] batch output dir: $BATCH_OUTPUT_DIR"
echo "[INFO] debug root: ${DEBUG_ROOT:-<excel-sibling-default>}"
echo "[INFO] concurrency: $CONCURRENCY"
echo "[INFO] llm target field batch size: $FIELD_LINEAGE_LLM_BATCH_SIZE (complete ETL sent per batch)"
echo "[INFO] llm target field batch concurrency: $FIELD_LINEAGE_LLM_BATCH_CONCURRENCY"
echo "[INFO] retry on failure: $RETRY_COUNT"
echo "[INFO] auto import AUTO_APPROVED: $([[ "$AUTO_IMPORT" -eq 1 ]] && echo yes || echo no)"
echo "[INFO] auto import mode: $([[ "$AUTO_IMPORT_CLEAR_EXISTING" -eq 1 ]] && echo clear_import || echo merge_update)"
echo "[INFO] auto import require full auto approved: $([[ "$AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED" -eq 1 ]] && echo yes || echo no)"
echo "[INFO] table count: ${#TABLE_LIST[@]}"
echo "[INFO] tables: ${TABLE_LIST[*]}"
echo "[INFO] gms url: $DATAHUB_GMS_URL"
echo "[INFO] gms token: $([[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && echo set || echo empty)"
echo "[INFO] llm provider: $LLM_PROVIDER"
echo "[INFO] llm model: ${LLM_MODEL:-<provider-default>}"
_llm_key_status() {
  case "$LLM_PROVIDER" in
    deepseek) [[ -n "${DEEPSEEK_API_KEY:-}" ]] && echo set || echo empty ;;
    openrouter) [[ -n "${OPENROUTER_API_KEY:-}" ]] && echo set || echo empty ;;
    *) [[ -n "${BLF_LLM_API_KEY:-}" ]] && echo set || echo empty ;;
  esac
}
echo "[INFO] llm api key: $(_llm_key_status)"
echo "[INFO] python: $PYTHON"
echo "[INFO] pythonpath: $PYTHONPATH_ROOT"

_run_cli() {
  PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" -m job_info_sync_datahub.field_lineage_cli "$@"
}

_log() {
  local table="$1"
  shift
  echo "[INFO][$table] $*"
}

_warn() {
  local table="$1"
  shift
  echo "[WARN][$table] $*" >&2
}

_error() {
  local table="$1"
  shift
  echo "[ERROR][$table] $*" >&2
}

_progress_status_count() {
  local _status="$1"
  find "$STATUS_DIR" -maxdepth 1 -type f \( -name '*.status' -o -name '*.import_status' \) -exec cat {} \; 2>/dev/null \
    | awk -v status="$_status" '$0 == status { count++ } END { print count + 0 }'
}

_print_progress() {
  local table="$1"
  local status_file="$2"
  local _lock_dir="$STATUS_DIR/progress.lock"
  local _status _status_label _done _ok_count _skip_count _fail_count
  local _import_done _import_ok_count _import_skip_count _import_fail_count

  while ! mkdir "$_lock_dir" 2>/dev/null; do
    sleep 0.1
  done
  trap 'rmdir "$_lock_dir" 2>/dev/null || true' RETURN

  _status="$(cat "$status_file" 2>/dev/null || echo 1)"
  case "$_status" in
    0) _status_label="ok" ;;
    2) _status_label="skipped" ;;
    *) _status_label="failed" ;;
  esac
  _done="$(find "$STATUS_DIR" -maxdepth 1 -type f -name '*.status' 2>/dev/null | wc -l | tr -d ' ')"
  _ok_count="$(_progress_status_count 0)"
  _skip_count="$(_progress_status_count 2)"
  _fail_count="$(_progress_status_count 1)"
  _import_done="$(find "$STATUS_DIR" -maxdepth 1 -type f -name '*.import_status' 2>/dev/null | wc -l | tr -d ' ')"
  _import_ok_count="$(_progress_status_count import:0)"
  _import_skip_count="$(_progress_status_count import:2)"
  _import_fail_count="$(_progress_status_count import:1)"
  echo "[PROGRESS] field lineage export ${_done}/${#TABLE_LIST[@]} done, ok=${_ok_count}, skipped=${_skip_count}, failed=${_fail_count}, latest=${table}, status=${_status_label}; auto_import ${_import_done}/${#TABLE_LIST[@]} done, ok=${_import_ok_count}, skipped=${_import_skip_count}, failed=${_import_fail_count}"
}

_json_get_import_meta() {
  local plan_file="$1"
  PYTHONPATH="$PYTHONPATH_ROOT" "$PYTHON" -c '
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    payload = json.load(f)
tables = payload.get("write_result", {}).get("tables", {})
is_complete = False
marked = False
covered = 0
schema_count = 0
missing_count = 0
field_count = 0
for table_result in tables.values():
    if not isinstance(table_result, dict):
        continue
    field_count += int(table_result.get("field_count", 0) or 0)
    completeness = table_result.get("field_lineage_completeness")
    if isinstance(completeness, dict):
        is_complete = bool(completeness.get("is_complete"))
        marked = bool(completeness.get("marked_data_availability_flag"))
        covered = int(completeness.get("covered_field_count", 0) or 0)
        schema_count = int(completeness.get("schema_field_count", 0) or 0)
        missing = completeness.get("missing_fields")
        missing_count = len(missing) if isinstance(missing, list) else 0
print(f"{int(is_complete)} {int(marked)} {covered} {schema_count} {missing_count} {field_count}")
' "$plan_file"
}

_auto_import_one_table() {
  local TABLE_NAME="$1"
  local OUTPUT_FILE="$2"
  local IMPORT_STATUS_FILE="$3"
  local IMPORT_PLAN_FILE="${OUTPUT_FILE%.xlsx}_auto_import_plan.json"
  local _cli_out _cli_rc _skip_reason _clear_arg _require_full_arg
  local _is_complete _marked _covered _schema_count _missing_count _field_count

  rm -f "$IMPORT_STATUS_FILE" "${IMPORT_STATUS_FILE}.skip_reason" "$IMPORT_PLAN_FILE"
  if [[ "$AUTO_IMPORT" -ne 1 ]]; then
    echo "auto import disabled" >"${IMPORT_STATUS_FILE}.skip_reason"
    echo "import:2" >"$IMPORT_STATUS_FILE"
    return 0
  fi

  _log "$TABLE_NAME" "auto import AUTO_APPROVED started -> $IMPORT_PLAN_FILE"
  _cli_out="$(mktemp "${TMPDIR:-/tmp}/field_lineage_import_cli.XXXXXX")"
  if [[ "$AUTO_IMPORT_CLEAR_EXISTING" -eq 1 ]]; then
    _clear_arg="--clear-existing"
  else
    _clear_arg="--no-clear-existing"
  fi
  _require_full_arg="--no-require-full-auto-approved"
  if [[ "$AUTO_IMPORT_REQUIRE_FULL_AUTO_APPROVED" -eq 1 ]]; then
    _require_full_arg="--require-full-auto-approved"
  fi

  set +e
  _run_cli \
    import-reviewed \
    --input "$OUTPUT_FILE" \
    --output "$IMPORT_PLAN_FILE" \
    --gms-url "$DATAHUB_GMS_URL" \
    --write \
    --import-statuses AUTO_APPROVED \
    "$_clear_arg" \
    "$_require_full_arg" \
    2>&1 7>&- | tee "$_cli_out"
  _cli_rc=${PIPESTATUS[0]}
  set -e

  if [[ "$_cli_rc" -eq 0 ]]; then
    if [[ -f "$IMPORT_PLAN_FILE" ]]; then
      _import_meta="$(_json_get_import_meta "$IMPORT_PLAN_FILE")"
      read -r _is_complete _marked _covered _schema_count _missing_count _field_count <<< "$_import_meta"
      echo "complete=$_is_complete marked=$_marked covered=$_covered schema=$_schema_count missing=$_missing_count imported_fields=$_field_count" >"${IMPORT_STATUS_FILE}.meta"
      _log "$TABLE_NAME" "auto import ok: imported_fields=${_field_count}, completeness=${_is_complete}, covered=${_covered}/${_schema_count}, missing=${_missing_count}, marked_flag=${_marked}, plan=$IMPORT_PLAN_FILE"
    else
      _log "$TABLE_NAME" "auto import ok: plan not found: $IMPORT_PLAN_FILE"
    fi
    rm -f "$_cli_out"
    echo "import:0" >"$IMPORT_STATUS_FILE"
    return 0
  fi

  if [[ "$_cli_rc" -eq 4 || "$_cli_rc" -eq 6 ]]; then
    _skip_reason="$(grep '^FIELD_LINEAGE_SKIP_REASON=' "$_cli_out" | tail -1 | sed 's/^FIELD_LINEAGE_SKIP_REASON=//')"
    if [[ -z "$_skip_reason" && "$_cli_rc" -eq 4 ]]; then
      _skip_reason="AUTO_APPROVED 行为空，等待人工审核"
    fi
    if [[ -z "$_skip_reason" ]]; then
      _skip_reason="未满足自动导入条件，等待人工审核"
    fi
    rm -f "$_cli_out"
    _warn "$TABLE_NAME" "auto import skipped: $_skip_reason"
    echo "$_skip_reason" >"${IMPORT_STATUS_FILE}.skip_reason"
    echo "import:2" >"$IMPORT_STATUS_FILE"
    return 0
  fi

  rm -f "$_cli_out"
  _error "$TABLE_NAME" "auto import failed: rc=$_cli_rc"
  echo "import:1" >"$IMPORT_STATUS_FILE"
  return 1
}

_export_one_table() {
  local TABLE_NAME="$1"
  local OUTPUT_FILE="$2"
  local STATUS_FILE="$3"
  local IMPORT_STATUS_FILE="$4"
  local max_attempts=$((RETRY_COUNT + 1))
  local attempt=1
  local _cli_out _cli_rc _skip_reason
  local DEBUG_DIR

  rm -f "$OUTPUT_FILE" "${STATUS_FILE}.skip_reason"
  while [[ "$attempt" -le "$max_attempts" ]]; do
    if [[ "$attempt" -gt 1 ]]; then
      _warn "$TABLE_NAME" "LLM/export 失败，第 ${attempt}/${max_attempts} 次重试 ..."
      rm -f "$OUTPUT_FILE"
    else
      _log "$TABLE_NAME" "export started -> $OUTPUT_FILE"
    fi

    _cli_out="$(mktemp "${TMPDIR:-/tmp}/field_lineage_cli.XXXXXX")"
    set +e
    _export_args=(
      export
      --table "$TABLE_NAME" \
      --output "$OUTPUT_FILE" \
      --gms-url "$DATAHUB_GMS_URL" \
      --llm-timeout-sec "$LLM_TIMEOUT" \
      --preview-chars "$PREVIEW_CHARS" \
      --llm-provider "$LLM_PROVIDER"
    )
    if [[ -n "$LLM_MODEL" ]]; then
      _export_args+=(--llm-model "$LLM_MODEL")
    fi
    if [[ -n "$DEBUG_ROOT" ]]; then
      DEBUG_DIR="$DEBUG_ROOT/$BATCH_ID/$TABLE_NAME"
      mkdir -p "$DEBUG_DIR"
      _export_args+=(--debug-dir "$DEBUG_DIR")
    fi
    _run_cli "${_export_args[@]}" 2>&1 7>&- | tee "$_cli_out"
    _cli_rc=${PIPESTATUS[0]}
    set -e

    if [[ "$_cli_rc" -eq 0 ]]; then
      rm -f "$_cli_out"
      _log "$TABLE_NAME" "export ok: $OUTPUT_FILE"
      echo 0 >"$STATUS_FILE"
      _auto_import_one_table "$TABLE_NAME" "$OUTPUT_FILE" "$IMPORT_STATUS_FILE" || true
      return 0
    fi

    if [[ "$_cli_rc" -eq 3 ]]; then
      _skip_reason="$(grep '^FIELD_LINEAGE_SKIP_REASON=' "$_cli_out" | tail -1 | sed 's/^FIELD_LINEAGE_SKIP_REASON=//')"
      rm -f "$_cli_out" "$OUTPUT_FILE"
      _warn "$TABLE_NAME" "${_skip_reason:-structured property 无内容，已跳过}"
      echo "${_skip_reason:-structured property 无内容}" >"${STATUS_FILE}.skip_reason"
      echo 2 >"$STATUS_FILE"
      echo "export skipped" >"${IMPORT_STATUS_FILE}.skip_reason"
      echo "import:2" >"$IMPORT_STATUS_FILE"
      return 0
    fi

    rm -f "$_cli_out"
    attempt=$((attempt + 1))
  done

  _error "$TABLE_NAME" "export failed after $max_attempts attempt(s)"
  echo 1 >"$STATUS_FILE"
  echo "export failed" >"${IMPORT_STATUS_FILE}.skip_reason"
  echo "import:2" >"$IMPORT_STATUS_FILE"
  return 1
}

# 并发槽位（bash 3+ 可用）
_SEM_FIFO="$STATUS_DIR/sem.fifo"
mkfifo "$_SEM_FIFO"
exec 7<>"$_SEM_FIFO"
_FD7_OPEN=1
rm -f "$_SEM_FIFO"
for ((_i = 0; _i < CONCURRENCY; _i++)); do echo >&7; done

for TABLE_NAME in "${TABLE_LIST[@]}"; do
  OUTPUT_FILE="$BATCH_OUTPUT_DIR/${BATCH_ID}_${TABLE_NAME}.xlsx"
  STATUS_FILE="$STATUS_DIR/${TABLE_NAME//./_}.status"
  IMPORT_STATUS_FILE="$STATUS_DIR/${TABLE_NAME//./_}.import_status"

  read -r -u 7
  (
    _export_one_table "$TABLE_NAME" "$OUTPUT_FILE" "$STATUS_FILE" "$IMPORT_STATUS_FILE" || true
    _print_progress "$TABLE_NAME" "$STATUS_FILE"
    echo >&7
  ) &
  _pids+=("$!")
done

for _pid in "${_pids[@]}"; do
  if ! wait "$_pid"; then
    : # 单表失败已在 STATUS_FILE 记录
  fi
done
_WAIT_COMPLETED=1
exec 7<&-
exec 7>&-
_FD7_OPEN=0

_ok=0
_skip=0
_fail=0
_import_ok=0
_import_skip=0
_import_fail=0
_import_complete=0
_import_marked=0
_skipped_tables=()
_import_skipped_tables=()
_import_failed_tables=()
for TABLE_NAME in "${TABLE_LIST[@]}"; do
  STATUS_FILE="$STATUS_DIR/${TABLE_NAME//./_}.status"
  IMPORT_STATUS_FILE="$STATUS_DIR/${TABLE_NAME//./_}.import_status"
  if [[ ! -f "$STATUS_FILE" ]]; then
    _fail=$((_fail + 1))
  else
    case "$(cat "$STATUS_FILE")" in
    0) _ok=$((_ok + 1)) ;;
    2)
      _skip=$((_skip + 1))
      _reason=""
      if [[ -f "${STATUS_FILE}.skip_reason" ]]; then
        _reason="$(cat "${STATUS_FILE}.skip_reason")"
      fi
      _skipped_tables+=("${TABLE_NAME}: ${_reason:-structured property 无内容}")
      ;;
    *) _fail=$((_fail + 1)) ;;
    esac
  fi

  if [[ ! -f "$IMPORT_STATUS_FILE" ]]; then
    _import_skip=$((_import_skip + 1))
    _import_skipped_tables+=("${TABLE_NAME}: 未执行自动导入")
    continue
  fi
  case "$(cat "$IMPORT_STATUS_FILE")" in
    import:0)
      _import_ok=$((_import_ok + 1))
      if [[ -f "${IMPORT_STATUS_FILE}.meta" ]]; then
        if grep -q 'complete=1' "${IMPORT_STATUS_FILE}.meta"; then
          _import_complete=$((_import_complete + 1))
        fi
        if grep -q 'marked=1' "${IMPORT_STATUS_FILE}.meta"; then
          _import_marked=$((_import_marked + 1))
        fi
      fi
      ;;
    import:2)
      _import_skip=$((_import_skip + 1))
      _reason=""
      if [[ -f "${IMPORT_STATUS_FILE}.skip_reason" ]]; then
        _reason="$(cat "${IMPORT_STATUS_FILE}.skip_reason")"
      fi
      _import_skipped_tables+=("${TABLE_NAME}: ${_reason:-自动导入已跳过}")
      ;;
    *)
      _import_fail=$((_import_fail + 1))
      _import_failed_tables+=("$TABLE_NAME")
      ;;
  esac
done

echo "[INFO] field lineage export finished at $(date -Iseconds)"
echo "[INFO] batch id (auto): $BATCH_ID"
echo "[INFO] succeeded: ${_ok}/${#TABLE_LIST[@]}, skipped: ${_skip}, failed: ${_fail}"
echo "[INFO] auto import summary: succeeded=${_import_ok}/${#TABLE_LIST[@]}, skipped=${_import_skip}, failed=${_import_fail}, complete=${_import_complete}, marked_field_lineage_flag=${_import_marked}"

_write_batch_summary_once || true

if [[ "$_skip" -gt 0 ]]; then
  echo "[WARN] ========== 以下表已跳过字段血缘导出 =========="
  for _entry in "${_skipped_tables[@]}"; do
    echo "[WARN]   ${_entry}"
  done
  echo "[WARN] 若因 structured property 无内容跳过，请先在 DataHub 补全 Etl Script (blf.data.warehouse.etl_script) 后重新导出。"
fi

if [[ "$_import_skip" -gt 0 ]]; then
  echo "[WARN] ========== 以下表已跳过自动导入 =========="
  for _entry in "${_import_skipped_tables[@]}"; do
    echo "[WARN]   ${_entry}"
  done
fi

if [[ "$_import_fail" -gt 0 ]]; then
  echo "[ERROR] ========== 以下表自动导入失败 ==========" >&2
  for _entry in "${_import_failed_tables[@]}"; do
    echo "[ERROR]   ${_entry}" >&2
  done
fi

if [[ "$_fail" -gt 0 || "$_import_fail" -gt 0 ]]; then
  exit 1
fi
