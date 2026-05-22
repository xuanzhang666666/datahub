#!/usr/bin/env bash
# run_audit_missing_dataset_properties.sh — neo4j2：扫描缺 datasetProperties 的 Hive 表（Summary 显示全路径）
#
# 只读审计，不修改 DataHub 中任何 aspect。
#
# ── neo4j2 典型用法 ─────────────────────────────────────────────────────────
#   cd /data/datahub/scripts
#   bash run_audit_missing_dataset_properties.sh
#
#   # 或指定输出目录
#   OUT_DIR=/data/datahub/reports/missing_props_audit bash run_audit_missing_dataset_properties.sh
#
# ── 环境变量 ─────────────────────────────────────────────────────────────────
# DATAHUB_GMS_URL              GMS 地址；默认 http://127.0.0.1:8080（须确认是 datahub-gms 映射端口，不是前端）
# DATAHUB_GMS_TOKEN            可选
# BLF_DATAHUB_PLATFORM_INSTANCE  默认 blf-prod-hive
# OUT_DIR                      输出目录，默认 $REPORT_DIR/missing_props_audit
# PAGE_SIZE                    GraphQL scroll 每页条数，默认 100
# SCROLL_QUERY                 ES 查询前缀，默认 blf-prod-hive
# MAX_PAGES                    0=扫完全部；调试可设 5 只扫前 5 页
# LINEAGE_ENV_FILE / lineage.env
#
# ── 产出 ─────────────────────────────────────────────────────────────────────
#   $OUT_DIR/missing_dataset_properties_tables.txt   库.表，一行一个（给 minimal 批量修复）
#   $OUT_DIR/missing_dataset_properties.jsonl        含 urn、原因等详情
#   $OUT_DIR/audit_summary.txt                       本次统计摘要
#
set -euo pipefail

# 部署路径：/data/datahub/scripts/run_audit_missing_dataset_properties.sh
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

if [[ -n "${LINEAGE_REPORT_DIR:-}" ]]; then
  REPORT_DIR="$LINEAGE_REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  REPORT_DIR="$WORKSPACE/lineage_reports"
else
  REPORT_DIR="$SCRIPT_DIR/lineage_reports"
fi
mkdir -p "$REPORT_DIR"

OUT_DIR="${OUT_DIR:-$REPORT_DIR/missing_props_audit}"
PAGE_SIZE="${PAGE_SIZE:-100}"
SCROLL_QUERY="${SCROLL_QUERY:-blf-prod-hive}"
MAX_PAGES="${MAX_PAGES:-0}"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="${LINEAGE_PYTHON}"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

echo "==================================================================="
echo " Audit: Hive datasets missing datasetProperties (read-only)"
echo " date=$(date -Iseconds 2>/dev/null || date)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR"
echo " OUT_DIR=$OUT_DIR"
echo " PAGE_SIZE=$PAGE_SIZE SCROLL_QUERY=$SCROLL_QUERY MAX_PAGES=$MAX_PAGES"
echo "==================================================================="

for _cand in "${LINEAGE_ENV_FILE:-}" "$SCRIPT_DIR/lineage.env" \
  "/data/datahub/scripts/lineage.env" \
  ${WORKSPACE:+"$WORKSPACE/lineage.env"}; do
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

GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
GMS_URL="${GMS_URL%/}"

if [[ ! -f "$PKG_DIR/audit_missing_dataset_properties.py" ]]; then
  echo "ERROR: 未找到 $PKG_DIR/audit_missing_dataset_properties.py" >&2
  echo "  请部署 xander/python/job_info_sync_datahub 到 JOB_INFO_SYNC_DIR（如 /data/datahub/scripts/job_info_sync_datahub）" >&2
  exit 1
fi

echo "[INFO] 检查 GMS 连通: $GMS_URL"
_HTTP_CODE=$(curl -sS -m 15 -o /dev/null -w "%{http_code}" \
  -X POST "$GMS_URL/api/graphql" \
  -H "Content-Type: application/json" \
  -d '{"query":"query { __typename }"}' || echo "000")
if [[ "$_HTTP_CODE" != "200" ]]; then
  echo "ERROR: GMS GraphQL 不可用 (http=$_HTTP_CODE) url=$GMS_URL" >&2
  echo "  请 docker ps 确认 datahub-gms 映射端口，勿把前端 8080 当成 GMS。" >&2
  exit 1
fi
echo "[INFO] GMS OK"

mkdir -p "$OUT_DIR"

ARGS=(
  --gms-url "$GMS_URL"
  --out-dir "$OUT_DIR"
  --page-size "$PAGE_SIZE"
  --scroll-query "$SCROLL_QUERY"
)
[[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && ARGS+=(--platform-instance "$BLF_DATAHUB_PLATFORM_INSTANCE")
[[ -n "${MAX_PAGES:-}" && "${MAX_PAGES}" != "0" ]] && ARGS+=(--max-pages "$MAX_PAGES")

# 勿用 python -m：neo4j2 上 /root/job_info_sync_datahub 会遮蔽 /data/datahub/scripts 下的包
_AUDIT_PY="$PKG_DIR/audit_missing_dataset_properties.py"
echo "[INFO] $_AUDIT_PY ${ARGS[*]}"

set +e
PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" "$_AUDIT_PY" "${ARGS[@]}" \
  2>&1 | tee "$OUT_DIR/audit_run.log"
_RC=${PIPESTATUS[0]}
set -e

if [[ $_RC -ne 0 ]]; then
  echo "[ERROR] 审计失败 exit=$_RC，日志: $OUT_DIR/audit_run.log" >&2
  exit "$_RC"
fi

_TABLES_FILE="$OUT_DIR/missing_dataset_properties_tables.txt"
_JSONL_FILE="$OUT_DIR/missing_dataset_properties.jsonl"
_COUNT=0
if [[ -f "$_TABLES_FILE" ]]; then
  _COUNT=$(grep -c . "$_TABLES_FILE" 2>/dev/null || echo 0)
fi

{
  echo "audit_time=$(date -Iseconds 2>/dev/null || date)"
  echo "gms_url=$GMS_URL"
  echo "platform_instance=${BLF_DATAHUB_PLATFORM_INSTANCE:-blf-prod-hive}"
  echo "scroll_query=$SCROLL_QUERY"
  echo "page_size=$PAGE_SIZE"
  echo "need_fix_count=$_COUNT"
  echo "tables_file=$_TABLES_FILE"
  echo "jsonl_file=$_JSONL_FILE"
} > "$OUT_DIR/audit_summary.txt"

echo "==================================================================="
echo "[DONE] 缺 datasetProperties、建议 minimal 修复的表: $_COUNT 张"
echo "[DONE] 表清单: $_TABLES_FILE"
echo "[DONE] 详情:   $_JSONL_FILE"
echo "[DONE] 摘要:   $OUT_DIR/audit_summary.txt"
echo ""
echo "下一步（只增不改，不删血缘/结构化属性）:"
echo "  export TABLE_FILE=$_TABLES_FILE"
echo "  export BLF_HIVE_INGEST_MODE=minimal"
echo "  export EXISTING_DATASET_ACTION=update"
echo "  bash run_jenkins_hive_table_ingest_from_tables.sh"
echo "==================================================================="
