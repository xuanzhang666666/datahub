#!/usr/bin/env bash
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
# Debug 中间产物默认写入 Jenkins WORKSPACE：
#   $WORKSPACE/field_lineage_debug/{批次号}/{库.表}/
# 未设置 WORKSPACE 时使用 CLI 默认值：Excel 同目录的 *_debug/
#   批次号每次运行自动生成 YYYYMMDDHHmm，仅打印在日志中
#
# ── 并发与重试 ───────────────────────────────────────────────────────────────
# FIELD_LINEAGE_CONCURRENCY  并发请求 LLM 数，默认 10
# FIELD_LINEAGE_RETRY_COUNT    失败后额外重试次数，默认 1（共最多 2 次）
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
RETRY_COUNT="${FIELD_LINEAGE_RETRY_COUNT:-1}"

if ! [[ "$CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: FIELD_LINEAGE_CONCURRENCY 必须为正整数: $CONCURRENCY" >&2
  exit 2
fi
if ! [[ "$RETRY_COUNT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: FIELD_LINEAGE_RETRY_COUNT 必须为非负整数: $RETRY_COUNT" >&2
  exit 2
fi

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
# shellcheck disable=SC2064
trap 'rm -rf "$STATUS_DIR"' EXIT

echo "[INFO] field lineage export started at $(date -Iseconds)"
echo "[INFO] batch id (auto): $BATCH_ID"
echo "[INFO] output root: $OUTPUT_DIR"
echo "[INFO] batch output dir: $BATCH_OUTPUT_DIR"
echo "[INFO] debug root: ${DEBUG_ROOT:-<excel-sibling-default>}"
echo "[INFO] concurrency: $CONCURRENCY"
echo "[INFO] retry on failure: $RETRY_COUNT"
echo "[INFO] table count: ${#TABLE_LIST[@]}"
echo "[INFO] tables: ${TABLE_LIST[*]}"
echo "[INFO] gms url: $DATAHUB_GMS_URL"
echo "[INFO] gms token: $([[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && echo set || echo empty)"
echo "[INFO] llm provider: $LLM_PROVIDER"
echo "[INFO] llm model: ${LLM_MODEL:-<provider-default>}"
echo "[INFO] llm api key: $([[ "$LLM_PROVIDER" == "deepseek" ]] && { [[ -n "${DEEPSEEK_API_KEY:-}" ]] && echo set || echo empty; } || { [[ -n "${BLF_LLM_API_KEY:-}" ]] && echo set || echo empty; })"
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

_export_one_table() {
  local TABLE_NAME="$1"
  local OUTPUT_FILE="$2"
  local STATUS_FILE="$3"
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
    _run_cli "${_export_args[@]}" >"$_cli_out" 2>&1
    _cli_rc=$?
    set -e
    cat "$_cli_out"

    if [[ "$_cli_rc" -eq 0 ]]; then
      rm -f "$_cli_out"
      _log "$TABLE_NAME" "export ok: $OUTPUT_FILE"
      echo 0 >"$STATUS_FILE"
      return 0
    fi

    if [[ "$_cli_rc" -eq 3 ]]; then
      _skip_reason="$(grep '^FIELD_LINEAGE_SKIP_REASON=' "$_cli_out" | tail -1 | sed 's/^FIELD_LINEAGE_SKIP_REASON=//')"
      rm -f "$_cli_out" "$OUTPUT_FILE"
      _warn "$TABLE_NAME" "${_skip_reason:-structured property 无内容，已跳过}"
      echo "${_skip_reason:-structured property 无内容}" >"${STATUS_FILE}.skip_reason"
      echo 2 >"$STATUS_FILE"
      return 0
    fi

    rm -f "$_cli_out"
    attempt=$((attempt + 1))
  done

  _error "$TABLE_NAME" "export failed after $max_attempts attempt(s)"
  echo 1 >"$STATUS_FILE"
  return 1
}

# 并发槽位（bash 3+ 可用）
_SEM_FIFO="$STATUS_DIR/sem.fifo"
mkfifo "$_SEM_FIFO"
exec 7<>"$_SEM_FIFO"
rm -f "$_SEM_FIFO"
for ((_i = 0; _i < CONCURRENCY; _i++)); do echo >&7; done

_pids=()
for TABLE_NAME in "${TABLE_LIST[@]}"; do
  OUTPUT_FILE="$BATCH_OUTPUT_DIR/${BATCH_ID}_${TABLE_NAME}.xlsx"
  STATUS_FILE="$STATUS_DIR/${TABLE_NAME//./_}.status"

  read -r -u 7
  (
    _export_one_table "$TABLE_NAME" "$OUTPUT_FILE" "$STATUS_FILE" || true
    echo >&7
  ) &
  _pids+=("$!")
done

for _pid in "${_pids[@]}"; do
  if ! wait "$_pid"; then
    : # 单表失败已在 STATUS_FILE 记录
  fi
done
exec 7>&-

_ok=0
_skip=0
_fail=0
_skipped_tables=()
for TABLE_NAME in "${TABLE_LIST[@]}"; do
  STATUS_FILE="$STATUS_DIR/${TABLE_NAME//./_}.status"
  if [[ ! -f "$STATUS_FILE" ]]; then
    _fail=$((_fail + 1))
    continue
  fi
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
done

echo "[INFO] field lineage export finished at $(date -Iseconds)"
echo "[INFO] batch id (auto): $BATCH_ID"
echo "[INFO] succeeded: ${_ok}/${#TABLE_LIST[@]}, skipped: ${_skip}, failed: ${_fail}"

if [[ "$_skip" -gt 0 ]]; then
  echo "[WARN] ========== 以下表已跳过字段血缘解析（structured property 无内容）=========="
  for _entry in "${_skipped_tables[@]}"; do
    echo "[WARN]   ${_entry}"
  done
  echo "[WARN] 请先在 DataHub 补全 Etl Script (blf.data.warehouse.etl_script) 后重新导出。"
fi

if [[ "$_fail" -gt 0 ]]; then
  exit 1
fi
