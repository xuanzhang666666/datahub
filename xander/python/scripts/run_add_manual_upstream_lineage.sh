#!/usr/bin/env bash
# run_add_manual_upstream_lineage.sh — Jenkins / 服务器：追加一条 Hive 表级上游血缘（合并已有 upstreamLineage）
#
# 用法与 run_batch_lineage_sync.sh 一致：在「Execute shell」里先 export 再执行本脚本，无需手写 PYTHONPATH。
#
# ── 必填 ──────────────────────────────────────────────────────────────────────
# TABLE_NAME         下游表：必须为 库.表，如 dw.dw_order_v1
# UPSTREAM_NAME      上游表：必须为 库.表，如 dw.dw_order_v1_archive
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
# REPLACE=1          仅保留本条上游（清空其余表级/字段级血缘，慎用）
# DRY_RUN=1          只打印计划，不写 GMS
# BLF_LINEAGE_SKIP_UPSTREAM_INGEST=1  上游表不在 DataHub 时不自动 Hive ingest（默认会先 ingest 再写血缘）
# HMS_THRIFT_HOST / HMS_THRIFT_PORT   Hive ingest 用，默认 hiveserver5.dp.data.bj1.wormpex.com:9083
# BLF_HIVE_INGEST_TIMEOUT_SEC         单表 ingest 超时秒数，默认 600
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
UPSTREAM_NAME="${UPSTREAM_NAME:-}"
if [[ -z "$TABLE_NAME" || -z "$UPSTREAM_NAME" ]]; then
  echo "ERROR: 请设置 TABLE_NAME 与 UPSTREAM_NAME（Jenkins 参数或环境变量）。" >&2
  exit 2
fi
if [[ "$TABLE_NAME" != *.* ]] || [[ "$UPSTREAM_NAME" != *.* ]]; then
  echo "ERROR: TABLE_NAME 与 UPSTREAM_NAME 必须为「库.表」格式（至少含一个点），例如 dw.dw_order_v1。" >&2
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
echo " PKG_DIR=$PKG_DIR  TABLE_NAME=$TABLE_NAME  UPSTREAM_NAME=$UPSTREAM_NAME"
echo " DRY_RUN=${DRY_RUN:-0}  REPLACE=${REPLACE:-0}"
echo "==================================================================="

ARGS=(--table-name "$TABLE_NAME" --upstream-name "$UPSTREAM_NAME")
[[ -n "${DATAHUB_GMS_URL:-}" ]] && ARGS+=(--datahub-gms "$DATAHUB_GMS_URL")
[[ -n "${DATAHUB_GMS_TOKEN:-}" ]] && ARGS+=(--token "$DATAHUB_GMS_TOKEN")
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${DATAHUB_ENV:-}" ]] && ARGS+=(--env "$DATAHUB_ENV")
[[ "${REPLACE:-0}" == "1" ]] && ARGS+=(--replace)
[[ "${DRY_RUN:-0}" == "1" ]] && ARGS+=(--dry-run)

echo "[INFO] PYTHON=$PYTHON PYTHONPATH=$PYTHONPATH_ROOT"
echo "[INFO] $PYTHON -m job_info_sync_datahub.manual_upstream_lineage ${ARGS[*]}"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.manual_upstream_lineage "${ARGS[@]}"

echo "[DONE] manual upstream lineage"
