#!/usr/bin/env bash
# run_batch_table_documentation_all_from_datasets.sh — 全量生成 DataHub table Documentation
#
# 逻辑：
#   1. 从 DataHub MySQL metadata_aspect_v2 中找出 structuredProperties 有
#      blf.data.warehouse.etl_script 或 blf.data.schedule.execute_shell 内容的 Hive dataset。
#   2. 过滤 viewProperties，只保留 table dataset。
#   3. 按 db.table 排序后，复用 run_batch_table_documentation_from_datasets.sh 批量生成 Documentation。
#
# Jenkins 参数：
#   DOC_WRITE_ACTION  append=保留人工文档并追加/替换自动区块（默认）；overwrite=整体覆盖
#   DRY_RUN           1=只生成报告和 Markdown 预览，不写 DataHub（默认）；0=写入 DataHub
#   CONCURRENCY       并发数（默认 10）
#   LLM_TIMEOUT       LLM 超时秒数（默认 300）
#   MAX_CONSECUTIVE_LLM_FAILURES  连续多少个 LLM_ERROR 后停止（默认 3；<=0 不启用）
#   LINEAGE_PYTHON    Python 解释器
#   DATAHUB_GMS_URL   GMS 地址
#   DATAHUB_GMS_TOKEN GMS token
#   DATAHUB_MYSQL_*   MySQL 连接：
#                     - 设 DATAHUB_MYSQL_HOST（及可选 PORT）时用本机 mysql 客户端直连（Jenkins 无需 docker）
#                     - 未设 HOST 时默认 docker exec DATAHUB_MYSQL_CONTAINER（需有 docker.sock 权限）
#                     还可覆盖 USER/PASSWORD/DATABASE/CLIENT（如 /opt/anaconda3/bin/mysql）
#   TABLE_PRE         表名前缀过滤，如 pdw → 只保留 *.pdw*（不含 pdw.xxx 库名前缀）（空=不过滤）
#   RESUME            1=断点续跑：复用已有表清单与 jsonl 报告，跳过 OK/SKIP 的表
#   FORCE_REDISCOVER  RESUME=1 时仍重新从 MySQL 发现表（默认 0，复用 table_names_all_document.txt）
#   SKIP_IF_LLM_DOC_EXISTS  1=Documentation 已含 LLM 生成内容时跳过（不调用 LLM、不写入）
#   TRINO_*           查询 SHOW CREATE TABLE 使用
set -euo pipefail

CONCURRENCY="${CONCURRENCY:-10}"
DRY_RUN="${DRY_RUN:-1}"
LLM_TIMEOUT="${LLM_TIMEOUT:-300}"
MAX_CONSECUTIVE_LLM_FAILURES="${MAX_CONSECUTIVE_LLM_FAILURES:-3}"
DOC_WRITE_ACTION="${DOC_WRITE_ACTION:-append}"
RESUME="${RESUME:-0}"
FORCE_REDISCOVER="${FORCE_REDISCOVER:-0}"
SKIP_IF_LLM_DOC_EXISTS="${SKIP_IF_LLM_DOC_EXISTS:-0}"
if [[ "$RESUME" == "1" ]]; then
  TABLE_LIST_CLEAR=0
else
  TABLE_LIST_CLEAR="${TABLE_LIST_CLEAR:-1}"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"
if [[ -n "${DOCUMENTATION_REPORT_DIR:-}" ]]; then
  REPORT_DIR="$DOCUMENTATION_REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  REPORT_DIR="$WORKSPACE/documentation_reports"
else
  REPORT_DIR="$SCRIPT_DIR/documentation_reports"
fi

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

mkdir -p "$REPORT_DIR"

_DISCOVERED_TABLES="$REPORT_DIR/table_names_all_document.txt"
_DISCOVERY_SUMMARY="$REPORT_DIR/table_documentation_full_discovery.json"

echo "==================================================================="
echo " Full batch table documentation from DataHub structured properties"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  PYTHONPATH_ROOT=$PYTHONPATH_ROOT"
echo " REPORT_DIR=$REPORT_DIR"
echo " CONCURRENCY=$CONCURRENCY  DRY_RUN=$DRY_RUN"
echo " DOC_WRITE_ACTION=$DOC_WRITE_ACTION"
echo " LLM_TIMEOUT=$LLM_TIMEOUT"
echo " MAX_CONSECUTIVE_LLM_FAILURES=$MAX_CONSECUTIVE_LLM_FAILURES"
echo " TABLE_PRE=${TABLE_PRE:-}"
echo " RESUME=$RESUME  FORCE_REDISCOVER=$FORCE_REDISCOVER"
echo " SKIP_IF_LLM_DOC_EXISTS=$SKIP_IF_LLM_DOC_EXISTS"
echo "==================================================================="

for _cand in "${LINEAGE_ENV_FILE:-}" "$SCRIPT_DIR/lineage.env" ${WORKSPACE:+"$WORKSPACE/lineage.env"}; do
  [[ -z "$_cand" ]] && continue
  if [[ -r "$_cand" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$_cand"; set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

if [[ "$DOC_WRITE_ACTION" != "append" && "$DOC_WRITE_ACTION" != "overwrite" ]]; then
  echo "ERROR: DOC_WRITE_ACTION 只能为 append 或 overwrite，当前为 $DOC_WRITE_ACTION" >&2
  exit 2
fi

if [[ ! -f "$PKG_DIR/table_documentation_full_discovery.py" ]]; then
  echo "ERROR: $PKG_DIR/table_documentation_full_discovery.py 不存在。" >&2
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/run_batch_table_documentation_from_datasets.sh" ]]; then
  echo "ERROR: $SCRIPT_DIR/run_batch_table_documentation_from_datasets.sh 不存在。" >&2
  exit 1
fi

if [[ "$RESUME" == "1" && "$FORCE_REDISCOVER" != "1" && -s "$_DISCOVERED_TABLES" ]]; then
  echo "[INFO] RESUME=1: 复用已有表清单 $_DISCOVERED_TABLES（设 FORCE_REDISCOVER=1 可重新发现）"
else
  echo "[INFO] discovering tables with Etl Script / Execute Shell structured properties ..."
  DISCOVERY_ARGS="--output $_DISCOVERED_TABLES --summary $_DISCOVERY_SUMMARY"
  [[ -n "${BLF_DATAHUB_PLATFORM_INSTANCE:-}" ]] && DISCOVERY_ARGS="$DISCOVERY_ARGS --platform-instance $BLF_DATAHUB_PLATFORM_INSTANCE"
  [[ -n "${DATAHUB_ENV:-}" ]] && DISCOVERY_ARGS="$DISCOVERY_ARGS --env $DATAHUB_ENV"
  [[ -n "${TABLE_PRE:-}" ]] && DISCOVERY_ARGS="$DISCOVERY_ARGS --table-prefix $TABLE_PRE"
  cd "$PYTHONPATH_ROOT"
  PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" -m job_info_sync_datahub.table_documentation_full_discovery $DISCOVERY_ARGS
fi

TABLE_COUNT="$(grep -cve '^[[:space:]]*$' "$_DISCOVERED_TABLES" || true)"
echo "[INFO] discovered table count: $TABLE_COUNT"
echo "[INFO] sorted table list: $_DISCOVERED_TABLES"
echo "[INFO] discovery summary: $_DISCOVERY_SUMMARY"
if [[ "$TABLE_COUNT" == "0" ]]; then
  echo "[DONE] 未发现 Etl Script / Execute Shell 有内容的 table dataset。"
  exit 0
fi

export TABLE_LIST_FILE="$_DISCOVERED_TABLES"
unset TABLE_NAMES
export CONCURRENCY DRY_RUN LLM_TIMEOUT MAX_CONSECUTIVE_LLM_FAILURES DOC_WRITE_ACTION TABLE_LIST_CLEAR RESUME SKIP_IF_LLM_DOC_EXISTS

echo "[INFO] running existing table documentation batch script ..."
sh "$SCRIPT_DIR/run_batch_table_documentation_from_datasets.sh"
