#!/usr/bin/env bash
# run_audit_datahub_table_lineage_quality.sh — 扫描 DataHub 当前 Hive 表级血缘质量
#
# 部署路径：/data/datahub/scripts/run_audit_datahub_table_lineage_quality.sh
#
# ── 可选环境变量 ─────────────────────────────────────────────────────────────
# DATAHUB_GMS_URL      GMS 地址，默认 http://localhost:8080
# DATAHUB_GMS_TOKEN    GMS token（无鉴权时可不填）
# LINEAGE_PYTHON       Python 解释器，默认 /opt/anaconda3/bin/python
# REPORT_DIR           报告目录，默认 $WORKSPACE/lineage_quality_reports 或 ./lineage_quality_reports
# LINEAGE_QUALITY_QUERY DataHub 搜索 query，默认 *
# MAX_DATASETS         最多扫描多少个 dataset；0 表示不限，默认 0
# BATCH_SIZE           DataHub scroll batch size，默认 2000
# HIVE_CHUNK_SIZE      Hive information_schema 分批大小，默认 1000
# CHECK_NO_UPSTREAM    设为 1 时检查非 ODS/源层表无上游血缘；可能产生较多结果
# DATAHUB_MYSQL_MODE   读取 view/Etl Script 结构化属性的 MySQL 方式，默认 host；可设 docker
# DATAHUB_MYSQL_BIN    宿主机 mysql 命令路径，默认自动查找或 /opt/anaconda3/bin/mysql
#
# 默认检查：
# - view dataset 必须有至少一个表级上游血缘
# - blf.data.warehouse.etl_script 有有效内容的表，如果表名前缀属于
#   dwa/dwd/pdim/dim/pdw/mid/dm/dw，则必须有至少一个表级上游血缘
# - ods/ai/app 前缀表不要求必须有上游血缘
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

GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
REPORT_DIR="${REPORT_DIR:-${WORKSPACE:-$(pwd)}/lineage_quality_reports}"
JSONL_PATH="$REPORT_DIR/datahub_table_lineage_quality.jsonl"
XLSX_PATH="$REPORT_DIR/datahub_table_lineage_quality.xlsx"

mkdir -p "$REPORT_DIR"

EXTRA_ARGS=()
if [[ -n "${DATAHUB_GMS_TOKEN:-}" ]]; then
    EXTRA_ARGS+=(--token "$DATAHUB_GMS_TOKEN")
fi
if [[ "${CHECK_NO_UPSTREAM:-0}" == "1" ]]; then
    EXTRA_ARGS+=(--include-no-upstream)
fi

echo "[INFO] audit DataHub table lineage quality started at $(date -Iseconds)"
echo "[INFO] gms url: $GMS_URL"
echo "[INFO] report dir: $REPORT_DIR"
echo "[INFO] python: $PYTHON"
echo "[INFO] ----------------------------------------"

(
    cd "$PYTHONPATH_ROOT"
    PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
        "$PYTHON" -m job_info_sync_datahub.audit_datahub_table_lineage_quality \
        --gms-url "$GMS_URL" \
        --jsonl "$JSONL_PATH" \
        --xlsx "$XLSX_PATH" \
        ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
)

echo "[INFO] ----------------------------------------"
echo "[INFO] audit DataHub table lineage quality finished at $(date -Iseconds)"
echo "[DONE] reports:"
echo "  jsonl: $JSONL_PATH"
echo "  xlsx:  $XLSX_PATH"
