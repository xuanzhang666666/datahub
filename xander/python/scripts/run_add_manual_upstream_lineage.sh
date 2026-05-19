#!/usr/bin/env bash
# run_add_manual_upstream_lineage.sh — Jenkins / 服务器：追加一批 Hive 表级上游血缘（合并已有 upstreamLineage）
#
# 用法与 run_batch_lineage_sync.sh 一致：在「Execute shell」里先 export 再执行本脚本，无需手写 PYTHONPATH。
#
# ── 必填 ──────────────────────────────────────────────────────────────────────
# TABLE_NAME         下游表：必须为 库.表，如 dw.dw_order_v1
# UPSTREAM_NAMES     上游表列表（Jenkins multi-line string 参数）：每行一个 库.表
#                    空行与 # 注释行自动跳过，例如：
#                      dw.dw_order_v1_archive
#                      ods.ods_order
#
# ── 常用（与批量血缘任务相同）────────────────────────────────────────────────
# LINEAGE_PYTHON     推荐 /opt/anaconda3/bin/python（Jenkins 非 root 勿用 /root/anaconda3）
# JOB_INFO_SYNC_DIR  job_info_sync_datahub 目录；默认 $SCRIPT_DIR/job_info_sync_datahub
# LINEAGE_ENV_FILE   优先加载的 env 文件；未设则尝试 $SCRIPT_DIR/lineage.env、$WORKSPACE/lineage.env
#                    （其中可写 DATAHUB_GMS_URL、DATAHUB_GMS_TOKEN 等）
#
# ── 可选 ──────────────────────────────────────────────────────────────────────
# DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN  未写 lineage.env 时可在此 export
# BLF_DATAHUB_PLATFORM_INSTANCE / DATAHUB_ENV  与 Python 模块默认值一致时可不设
# REPLACE=1          仅保留本次指定的上游（清空其余表级/字段级血缘，慎用）
# DRY_RUN=1          只打印计划，不写 GMS
# BLF_LINEAGE_SKIP_UPSTREAM_INGEST=1  上游不在 DataHub 时不注册/ingest（默认会轻量注册）
# BLF_LINEAGE_FULL_UPSTREAM_INGEST=1  改为完整 HMS ingest（慢，易超时；默认仅 MCP 轻量注册）
# BLF_HIVE_INGEST_TIMEOUT_SEC         完整 HMS ingest 超时（本脚本在加载 lineage.env 后固定为 86400=24h）
# HMS_THRIFT_HOST / HMS_THRIFT_PORT   仅完整 ingest 时需要
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

TABLE_NAME="${TABLE_NAME:-}"
UPSTREAM_NAMES="${UPSTREAM_NAMES:-}"
if [[ -z "$TABLE_NAME" || -z "$UPSTREAM_NAMES" ]]; then
  echo "ERROR: 请设置 TABLE_NAME 与 UPSTREAM_NAMES（Jenkins 参数或环境变量）。" >&2
  exit 2
fi
if [[ "$TABLE_NAME" != *.* ]]; then
  echo "ERROR: TABLE_NAME 必须为「库.表」格式（至少含一个点），例如 dw.dw_order_v1。" >&2
  exit 2
fi

# 解析多行 UPSTREAM_NAMES：去空白行与 # 注释行
UPSTREAM_LIST=()
while IFS= read -r _line; do
  _line="${_line//[$'\r']}"          # 去掉 Windows 换行符
  _trimmed="${_line#"${_line%%[![:space:]]*}"}"  # ltrim
  _trimmed="${_trimmed%"${_trimmed##*[![:space:]]}"}"  # rtrim
  [[ -z "$_trimmed" || "$_trimmed" == \#* ]] && continue
  if [[ "$_trimmed" != *.* ]]; then
    echo "ERROR: UPSTREAM_NAMES 中的 '$_trimmed' 必须为「库.表」格式（至少含一个点）。" >&2
    exit 2
  fi
  UPSTREAM_LIST+=("$_trimmed")
done <<< "$UPSTREAM_NAMES"

if [[ ${#UPSTREAM_LIST[@]} -eq 0 ]]; then
  echo "ERROR: UPSTREAM_NAMES 解析后为空，请检查参数内容。" >&2
  exit 2
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

# 完整 HMS ingest 易因 default 大库扫描很久；覆盖 lineage.env 里较小的值（如 600）
export BLF_HIVE_INGEST_TIMEOUT_SEC=86400

if ! "$PYTHON" -c "from datahub.ingestion.graph.client import DataHubGraph; from datahub.emitter.rest_emitter import DatahubRestEmitter" 2>/dev/null; then
  echo "ERROR: 需要 acryl-datahub（含 graph）。请: pip install 'acryl-datahub>=0.12' 或设置 LINEAGE_PYTHON。" >&2
  exit 1
fi

if [[ ! -f "$PKG_DIR/manual_upstream_lineage.py" ]]; then
  echo "ERROR: 未找到 $PKG_DIR/manual_upstream_lineage.py" >&2
  echo "  请部署代码或: export JOB_INFO_SYNC_DIR=/绝对路径/job_info_sync_datahub" >&2
  exit 1
fi

echo "==================================================================="
echo " Manual upstream lineage"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  TABLE_NAME=$TABLE_NAME"
printf ' UPSTREAM_NAMES (%d):\n' "${#UPSTREAM_LIST[@]}"
for _u in "${UPSTREAM_LIST[@]}"; do printf '   %s\n' "$_u"; done
echo " DRY_RUN=${DRY_RUN:-0}  REPLACE=${REPLACE:-0}"
echo " BLF_HIVE_INGEST_TIMEOUT_SEC=${BLF_HIVE_INGEST_TIMEOUT_SEC}"
echo " BLF_LINEAGE_FULL_UPSTREAM_INGEST=${BLF_LINEAGE_FULL_UPSTREAM_INGEST:-0}"
echo "==================================================================="

_BASE_ARGS=()
[[ -n "${DATAHUB_GMS_URL:-}" ]]                    && _BASE_ARGS+=(--datahub-gms "$DATAHUB_GMS_URL")
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]]                  && _BASE_ARGS+=(--token "$DATAHUB_GMS_TOKEN")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]]      && _BASE_ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]]                        && _BASE_ARGS+=(--env "$DATAHUB_ENV")
[[ "${REPLACE:-0}" == "1" ]]                       && _BASE_ARGS+=(--replace)
[[ "${DRY_RUN:-0}" == "1" ]]                       && _BASE_ARGS+=(--dry-run)

_ok=0; _fail=0
for _upstream in "${UPSTREAM_LIST[@]}"; do
  echo "[INFO] ----------------------------------------"
  echo "[INFO] upstream: $_upstream"
  ARGS=(--table-name "$TABLE_NAME" --upstream-name "$_upstream" "${_BASE_ARGS[@]}")
  echo "[INFO] $PYTHON -m job_info_sync_datahub.manual_upstream_lineage ${ARGS[*]}"
  if PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
       "$PYTHON" -m job_info_sync_datahub.manual_upstream_lineage "${ARGS[@]}"; then
    _ok=$((_ok + 1))
  else
    echo "[ERROR] 上游写入失败: $_upstream" >&2
    _fail=$((_fail + 1))
  fi
done

echo "==================================================================="
echo "[DONE] manual upstream lineage  succeeded=${_ok}  failed=${_fail}"
echo "==================================================================="
[[ $_fail -eq 0 ]]
