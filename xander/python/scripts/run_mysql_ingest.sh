#!/usr/bin/env bash
# run_mysql_ingest.sh — Jenkins/CLI：将 MySQL 库表元数据同步到 DataHub
#
# 必填环境变量：
#   CLUSTER_INFOS            可选；多行 CSV：集群名,服务器名称,IP,端口,角色。设置后按集群列表批量执行
#   MYSQL_SOURCE_NAME       数据源名，用于生成 recipe/report 文件名，如 finance_prod
#   MYSQL_HOST_PORT         MySQL host:port，如 mysql.example.com:3306
#   MYSQL_USERNAME          MySQL 只读账号
#   MYSQL_PASSWORD          MySQL 只读密码
#
# 常用可选环境变量：
#   MYSQL_DATABASE_ALLOW    database allow 正则，支持逗号或多行，如 ^app_db$
#   MYSQL_DATABASE_DENY     database deny 正则；默认排除 information_schema/mysql/performance_schema/sys
#   MYSQL_TABLE_ALLOW       table allow 正则，支持逗号或多行，如 ^app_db\..*$
#   MYSQL_PLATFORM_INSTANCE DataHub platform instance；默认使用 MYSQL_SOURCE_NAME（公司 MySQL 集群名）
#   MYSQL_INCLUDE_VIEWS     1=采集 view（默认），0=不采集
#   MYSQL_INCLUDE_TABLES    1=采集 table（默认），0=不采集
#   MYSQL_PROFILING_ENABLED 1=开启 profiling（默认 0，避免扫数据）
#   MYSQL_RECIPE_OUT        生成 recipe 路径，默认 $REPORT_DIR/mysql_<source>_to_datahub.yml
#   DATAHUB_GMS_URL         默认 http://localhost:8080
#   DATAHUB_GMS_TOKEN       可写在 lineage.env 或 Jenkins 凭据中
#   DATAHUB_ENV             默认 PROD
#   DRY_RUN                 1=只 preview，不写 DataHub
#   LINEAGE_PYTHON          推荐 /opt/anaconda3/bin/python
#   LINEAGE_ENV_FILE        可选 env 文件；默认也会尝试脚本同目录 lineage.env
set -euo pipefail

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

safe_name() {
  printf '%s' "$1" | sed 's/[^A-Za-z0-9_-]/_/g'
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"

if [[ -n "${LINEAGE_REPORT_DIR:-}" ]]; then
  REPORT_DIR="$LINEAGE_REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  REPORT_DIR="$WORKSPACE/lineage_reports"
else
  REPORT_DIR="$SCRIPT_DIR/lineage_reports"
fi
mkdir -p "$REPORT_DIR"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
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

# Jenkins 常以 wstats 等非 root 用户运行；pymysql 依赖 cryptography，在部分 OpenSSL 3
# 配置下需关闭 legacy provider，否则 import pymysql 失败且与 root 手动测试结果不一致。
export CRYPTOGRAPHY_OPENSSL_NO_LEGACY="${CRYPTOGRAPHY_OPENSSL_NO_LEGACY:-1}"

MYSQL_SOURCE_NAME="${MYSQL_SOURCE_NAME:-}"
MYSQL_HOST_PORT="${MYSQL_HOST_PORT:-}"
DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
DATAHUB_ENV="${DATAHUB_ENV:-PROD}"
MYSQL_INCLUDE_VIEWS="${MYSQL_INCLUDE_VIEWS:-1}"
MYSQL_INCLUDE_TABLES="${MYSQL_INCLUDE_TABLES:-1}"
MYSQL_PROFILING_ENABLED="${MYSQL_PROFILING_ENABLED:-0}"

if [[ -n "${CLUSTER_INFOS:-}" && "${_MYSQL_CLUSTER_BATCH_CHILD:-0}" != "1" ]]; then
  echo "==================================================================="
  echo " MySQL cluster batch ingest -> DataHub"
  echo " date=$(date -Iseconds)"
  echo " DATAHUB_GMS_URL=$DATAHUB_GMS_URL"
  echo " DATAHUB_ENV=$DATAHUB_ENV"
  echo " DRY_RUN=${DRY_RUN:-0}"
  echo "==================================================================="

  cluster_count=0
  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    line="$(trim "$raw_line")"
    [[ -z "$line" ]] && continue
    [[ "$line" == \#* ]] && continue

    IFS=',' read -r cluster_name server_name ip port role extra <<< "$line"
    cluster_name="$(trim "${cluster_name:-}")"
    server_name="$(trim "${server_name:-}")"
    ip="$(trim "${ip:-}")"
    port="$(trim "${port:-}")"
    role="$(trim "${role:-}")"

    if [[ "$cluster_name" == "集群名" ]]; then
      continue
    fi
    if [[ -n "${extra:-}" || -z "$cluster_name" || -z "$port" || ( -z "$server_name" && -z "$ip" ) ]]; then
      echo "ERROR: CLUSTER_INFOS 行格式非法: $raw_line" >&2
      echo "  期望格式: 集群名,服务器名称,IP,端口,角色" >&2
      exit 2
    fi

    host="$server_name"
    [[ -z "$host" ]] && host="$ip"
    safe_cluster="$(safe_name "$cluster_name")"
    cluster_count=$((cluster_count + 1))

    echo "-------------------------------------------------------------------"
    echo "[INFO] cluster #$cluster_count: name=$cluster_name host=$host port=$port role=${role:-<empty>}"
    echo "-------------------------------------------------------------------"

    CLUSTER_INFOS= \
    _MYSQL_CLUSTER_BATCH_CHILD=1 \
    MYSQL_SOURCE_NAME="$cluster_name" \
    MYSQL_HOST_PORT="$host:$port" \
    MYSQL_PLATFORM_INSTANCE="$cluster_name" \
    MYSQL_RECIPE_OUT="$REPORT_DIR/mysql_${safe_cluster}_to_datahub.yml" \
      bash "$0"
  done <<< "$CLUSTER_INFOS"

  if [[ "$cluster_count" -eq 0 ]]; then
    echo "ERROR: CLUSTER_INFOS 没有可执行的集群行" >&2
    exit 2
  fi

  echo "[DONE] mysql cluster batch ingest count=$cluster_count"
  exit 0
fi

if [[ -z "$MYSQL_SOURCE_NAME" ]]; then
  echo "ERROR: 请设置 MYSQL_SOURCE_NAME，例如 finance_prod" >&2
  exit 2
fi
if [[ -z "$MYSQL_HOST_PORT" ]]; then
  echo "ERROR: 请设置 MYSQL_HOST_PORT，例如 mysql.example.com:3306" >&2
  exit 2
fi
if [[ -z "${MYSQL_USERNAME:-}" || -z "${MYSQL_PASSWORD:-}" ]]; then
  echo "ERROR: 请设置 MYSQL_USERNAME / MYSQL_PASSWORD（建议放 Jenkins 凭据或 lineage.env）" >&2
  exit 2
fi

SAFE_SOURCE="$(safe_name "$MYSQL_SOURCE_NAME")"
MYSQL_PLATFORM_INSTANCE="${MYSQL_PLATFORM_INSTANCE:-$MYSQL_SOURCE_NAME}"
RECIPE_OUT="${MYSQL_RECIPE_OUT:-$REPORT_DIR/mysql_${SAFE_SOURCE}_to_datahub.yml}"

if ! _import_err="$("$PYTHON" -c "import datahub; import pymysql" 2>&1)"; then
  echo "ERROR: 需要 DataHub MySQL connector（解释器: $PYTHON）" >&2
  if [[ -n "$_import_err" ]]; then
    echo "  import 失败详情: $_import_err" >&2
  fi
  echo "  安装命令: $PYTHON -m pip install -U 'acryl-datahub[mysql]'" >&2
  echo "  若以非 root 用户运行且报 OpenSSL legacy provider，请: export CRYPTOGRAPHY_OPENSSL_NO_LEGACY=1" >&2
  exit 1
fi
if [[ ! -f "$PKG_DIR/mysql_ingest_recipe.py" ]]; then
  echo "ERROR: 未找到 $PKG_DIR/mysql_ingest_recipe.py，请部署 job_info_sync_datahub 包" >&2
  exit 1
fi

RENDER_ARGS=(
  --host-port "$MYSQL_HOST_PORT"
  --database-allow "${MYSQL_DATABASE_ALLOW:-}"
  --database-deny "${MYSQL_DATABASE_DENY:-^information_schema$,^mysql$,^performance_schema$,^sys$}"
  --table-allow "${MYSQL_TABLE_ALLOW:-}"
  --gms-url "$DATAHUB_GMS_URL"
  --platform-instance "$MYSQL_PLATFORM_INSTANCE"
  --env "$DATAHUB_ENV"
  --out "$RECIPE_OUT"
)
[[ "$MYSQL_INCLUDE_VIEWS" == "0" ]] && RENDER_ARGS+=(--no-include-views)
[[ "$MYSQL_INCLUDE_TABLES" == "0" ]] && RENDER_ARGS+=(--no-include-tables)
[[ "$MYSQL_PROFILING_ENABLED" == "1" ]] && RENDER_ARGS+=(--profiling-enabled)

echo "==================================================================="
echo " MySQL ingest -> DataHub"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " MYSQL_SOURCE_NAME=$MYSQL_SOURCE_NAME"
echo " MYSQL_HOST_PORT=$MYSQL_HOST_PORT"
echo " MYSQL_PLATFORM_INSTANCE=$MYSQL_PLATFORM_INSTANCE"
echo " DATAHUB_ENV=$DATAHUB_ENV"
echo " DATAHUB_GMS_URL=$DATAHUB_GMS_URL"
echo " MYSQL_DATABASE_ALLOW=${MYSQL_DATABASE_ALLOW:-<all>}"
echo " MYSQL_DATABASE_DENY=${MYSQL_DATABASE_DENY:-^information_schema$,^mysql$,^performance_schema$,^sys$}"
echo " MYSQL_TABLE_ALLOW=${MYSQL_TABLE_ALLOW:-<all>}"
echo " MYSQL_INCLUDE_TABLES=$MYSQL_INCLUDE_TABLES MYSQL_INCLUDE_VIEWS=$MYSQL_INCLUDE_VIEWS"
echo " MYSQL_PROFILING_ENABLED=$MYSQL_PROFILING_ENABLED"
echo " DRY_RUN=${DRY_RUN:-0}"
echo " RECIPE_OUT=$RECIPE_OUT"
echo "==================================================================="

PYTHONPATH="$PYTHONPATH_ROOT" "$PYTHON" -m job_info_sync_datahub.mysql_ingest_recipe "${RENDER_ARGS[@]}"
echo "[INFO] rendered recipe: $RECIPE_OUT"

DH_CMD=("$PYTHON" -m datahub ingest -c "$RECIPE_OUT")
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  DH_CMD+=(--preview)
fi
DH_CMD+=(--no-progress --no-spinner)

echo "[INFO] running: ${DH_CMD[*]}"
"${DH_CMD[@]}"

echo "[DONE] mysql ingest source=$MYSQL_SOURCE_NAME recipe=$RECIPE_OUT"
