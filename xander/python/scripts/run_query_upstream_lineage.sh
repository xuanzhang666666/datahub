#!/usr/bin/env bash
# run_query_upstream_lineage.sh — 查询 DataHub 表级血缘上游表，并校验结构化属性
#
# 部署路径：/data/datahub/scripts/run_query_upstream_lineage.sh
#
# ── Jenkins 参数 ──────────────────────────────────────────────────────────────
# TABLE_NAMES  （Multi-line String Parameter，必填）
#              一行一个目标表名，格式 库.表 或仅 表名（默认库 default）
#              例：
#                default.dim_store_info
#                default.dim_city_info
#                mid_order_info
#
# ── 可选环境变量 ─────────────────────────────────────────────────────────────
# DATAHUB_GMS_URL     GMS 地址，默认 http://localhost:8080
# DATAHUB_GMS_TOKEN   GMS token（无鉴权时可不填）
# LINEAGE_PYTHON      Python 解释器，默认 /opt/anaconda3/bin/python
# NO_CHECK_PROPS      设为 1 时跳过结构化属性检查
# UPSTREAM_LINEAGE_XLSX  上游明细 Excel 输出路径，默认在 Jenkins WORKSPACE 下生成
#
# ── 退出码 ────────────────────────────────────────────────────────────────────
# 0  正常；如有上游表缺少结构化属性，会按缺少项分组打印
# 1  查询 DataHub 失败
# 2  参数错误
set -euo pipefail

# ── 定位包目录 ────────────────────────────────────────────────────────────────
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

# ── Python 解释器 ─────────────────────────────────────────────────────────────
if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
    PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
    PYTHON=/opt/anaconda3/bin/python
else
    PYTHON=python3
fi

# ── 加载 lineage.env ──────────────────────────────────────────────────────────
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

export DATAHUB_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-eyJhbGciOiJIUzI1NiJ9.eyJhY3RvclR5cGUiOiJVU0VSIiwiYWN0b3JJZCI6ImRhdGFodWIiLCJ0eXBlIjoiUEVSU09OQUwiLCJ2ZXJzaW9uIjoiMiIsImp0aSI6IjgxMDY0Zjk0LWNmOWEtNGMzZS04MDU5LTExMzc5OTU1MzM5MCIsInN1YiI6ImRhdGFodWIiLCJpc3MiOiJkYXRhaHViLW1ldGFkYXRhLXNlcnZpY2UifQ.pPRncAU5T3P2PeP78q1f53KdS56rNZpeQJ8AUMjSbrw}"

# ── 参数解析：TABLE_NAMES（Multi-line String）────────────────────────────────
TABLE_NAMES="${TABLE_NAMES:-}"
if [[ -z "${TABLE_NAMES//[[:space:]]/}" ]]; then
    echo "ERROR: TABLE_NAMES 为空，请在 Jenkins Multi-line String Parameter 中填写目标表名（一行一个）。" >&2
    exit 2
fi

# 将多行表名拆分为 --table-name 参数列表
TABLE_NAME_ARGS=()
TABLE_NAMES="${TABLE_NAMES//$'\r'/}"
while IFS= read -r _line || [[ -n "$_line" ]]; do
    _line="${_line#"${_line%%[![:space:]]*}"}"
    _line="${_line%"${_line##*[![:space:]]}"}"
    [[ -z "$_line" ]] && continue
    TABLE_NAME_ARGS+=(--table-name "$_line")
done <<< "$TABLE_NAMES"

if [[ ${#TABLE_NAME_ARGS[@]} -eq 0 ]]; then
    echo "ERROR: TABLE_NAMES 未解析到有效表名。" >&2
    exit 2
fi

GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
REPORT_WORKSPACE="${WORKSPACE:-$PWD}"
UPSTREAM_LINEAGE_XLSX="${UPSTREAM_LINEAGE_XLSX:-$REPORT_WORKSPACE/upstream_lineage_details_$(date +%Y%m%d_%H%M%S).xlsx}"
mkdir -p "$(dirname "$UPSTREAM_LINEAGE_XLSX")"

# ── 构造额外参数 ──────────────────────────────────────────────────────────────
EXTRA_ARGS=()
if [[ -n "${DATAHUB_GMS_TOKEN:-}" ]]; then
    EXTRA_ARGS+=(--token "$DATAHUB_GMS_TOKEN")
fi
if [[ "${NO_CHECK_PROPS:-0}" == "1" ]]; then
    EXTRA_ARGS+=(--no-check-props)
fi

echo "[INFO] query upstream lineage started at $(date -Iseconds)"
echo "[INFO] table count: $(( ${#TABLE_NAME_ARGS[@]} / 2 ))"
echo "[INFO] gms url: $GMS_URL"
echo "[INFO] python: $PYTHON"
echo "[INFO] upstream detail xlsx: $UPSTREAM_LINEAGE_XLSX"
echo "[INFO] ----------------------------------------"

set +e
(
    cd "$PYTHONPATH_ROOT"
    PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
        "$PYTHON" -m job_info_sync_datahub.query_upstream_lineage \
        "${TABLE_NAME_ARGS[@]}" \
        --gms-url "$GMS_URL" \
        --output-xlsx "$UPSTREAM_LINEAGE_XLSX" \
        ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
)
EXIT_CODE=$?
set -e

if [[ "$EXIT_CODE" -eq 3 ]]; then
    echo "[INFO] 检测到旧版缺少结构化属性退出码 3，本次按报告完成处理，不标记失败。"
    EXIT_CODE=0
fi

echo "[INFO] ----------------------------------------"
echo "[INFO] query upstream lineage finished at $(date -Iseconds)"
exit $EXIT_CODE
