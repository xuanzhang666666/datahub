#!/usr/bin/env bash
# Run on neo4j2 (SSH/cron). HMS Thrift ingestion for data_finance only (see recipe YAML header).
#
# 默认在宿主机执行 ingest（与 run_batch_lineage_sync.sh 一致：优先 LINEAGE_PYTHON /opt/anaconda3），
# 不再依赖 docker exec 进 datahub-actions 容器，避免容器内存与 Jenkins 日志割裂问题。
#
# Deploy (flat names for put2 FTP):
#   cp xander/recipes/hive_metastore_data_finance.yml ./hive_metastore_data_finance.yml
#   cp xander/run/run_hive_data_finance_on_neo4j2.sh ./run_hive_data_finance_on_neo4j2.sh
#   put2 hive_metastore_data_finance.yml
#   put2 run_hive_data_finance_on_neo4j2.sh
# On neo4j2 after get2:
#   mv hive_metastore_data_finance.yml /data/datahub/recipes/
#   mv run_hive_data_finance_on_neo4j2.sh /data/datahub/scripts/ && chmod +x /data/datahub/scripts/run_hive_data_finance_on_neo4j2.sh
#
# Jenkins 示例（与血缘批处理一致）:
#   export LINEAGE_PYTHON=/opt/anaconda3/bin/python
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080   # 或 neo4j2 上可达的 GMS 地址
#   sh /data/datahub/scripts/run_hive_data_finance_on_neo4j2.sh
#
# 宿主机依赖（一次安装）:
#   $LINEAGE_PYTHON -m pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'
#   说明: 1.5.x 注册 hive-metastore 时会链式导入 classification，需 datahub_classify（随 presto-on-hive extra）。
#   官方 CLI 以 Python 3.11 为主测；3.12 可能告警但仍可跑，建议与 GMS 大版本对齐。
#
# Environment:
#   HMS_THRIFT_HOST HMS_THRIFT_PORT DATAHUB_GMS_URL DATAHUB_GMS_TOKEN(optional)
#   LINEAGE_PYTHON — 与 run_batch_lineage_sync.sh 相同，优先使用该解释器执行 python -m datahub
#   HIVE_INGEST_PYTHON — 若设置则覆盖 LINEAGE_PYTHON（仅本脚本）
#   HIVE_INGEST_USE_DOCKER=1 — 恢复旧逻辑：docker cp + docker exec 进 datahub-actions
#   DATAHUB_ACTIONS_CONTAINER — Docker 模式下指定容器名
#   DATAHUB_DOCKER_SUDO: auto|0|1（仅 Docker 模式）
#   TZ — default Asia/Shanghai
set -euo pipefail
RECIPE_DIR=/data/datahub/recipes
RECIPE_NAME=hive_metastore_data_finance.yml
HOST_RECIPE="$RECIPE_DIR/$RECIPE_NAME"
TMP_RECIPE="/tmp/$RECIPE_NAME"

export HMS_THRIFT_HOST="${HMS_THRIFT_HOST:-hiveserver5.dp.data.bj1.wormpex.com}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"
export TZ="${TZ:-Asia/Shanghai}"

# 解释器：HIVE_INGEST_PYTHON > LINEAGE_PYTHON > /opt/anaconda3 > 仅 root 时 /root/anaconda3 > python3
#（对齐 xander/python/scripts/run_batch_lineage_sync.sh）
if [[ -n "${HIVE_INGEST_PYTHON:-}" ]]; then
  PYTHON="$HIVE_INGEST_PYTHON"
elif [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

# ── Docker 模式（可选回退）──────────────────────────────────────────────────
if [[ "${HIVE_INGEST_USE_DOCKER:-0}" == "1" ]]; then
  _DH_DOCKER_MODE=direct
  case "${DATAHUB_DOCKER_SUDO:-auto}" in
    1|true|yes)
      if ! sudo -n docker info >/dev/null 2>&1; then
        echo "ERROR: DATAHUB_DOCKER_SUDO=1 but sudo -n docker is not permitted." >&2
        exit 1
      fi
      _DH_DOCKER_MODE=sudo
      ;;
    0|false|no)
      if ! docker info >/dev/null 2>&1; then
        echo "ERROR: docker failed and DATAHUB_DOCKER_SUDO=0 (no sudo)." >&2
        exit 1
      fi
      _DH_DOCKER_MODE=direct
      ;;
    *)
      if docker info >/dev/null 2>&1; then
        _DH_DOCKER_MODE=direct
      elif command -v sg >/dev/null 2>&1 && sg docker -c "docker info" >/dev/null 2>&1; then
        _DH_DOCKER_MODE=sg
      elif sudo -n docker info >/dev/null 2>&1; then
        _DH_DOCKER_MODE=sudo
      else
        echo "ERROR: cannot access Docker." >&2
        exit 1
      fi
      ;;
  esac

  dh_docker() {
    case "$_DH_DOCKER_MODE" in
      sudo) sudo -n docker "$@" ;;
      sg) sg docker -c "docker $(printf '%q ' "$@")" ;;
      *) docker "$@" ;;
    esac
  }

  CTR="${DATAHUB_ACTIONS_CONTAINER:-}"
  if [[ -z "$CTR" ]]; then
    for n in $(dh_docker ps --format '{{.Names}}'); do
      if [[ "$n" == *datahub-actions* ]]; then CTR="$n"; break; fi
    done
  fi
  if [[ -z "$CTR" ]]; then
    CTR=root-datahub-actions-1
  fi

  GMS="${DATAHUB_GMS_URL:-http://datahub-gms:8080}"
  dh_docker cp "$HOST_RECIPE" "$CTR:$TMP_RECIPE"
  dh_docker exec \
    -e TZ="$TZ" \
    -e PYTHONUNBUFFERED=1 \
    -e DATAHUB_TELEMETRY_ENABLED=false \
    -e HMS_THRIFT_HOST="$HMS_THRIFT_HOST" \
    -e HMS_THRIFT_PORT="$HMS_THRIFT_PORT" \
    -e DATAHUB_GMS_URL="$GMS" \
    -e DATAHUB_GMS_TOKEN="${DATAHUB_GMS_TOKEN:-}" \
    "$CTR" sh -c "datahub ingest -c \"$TMP_RECIPE\"; ec=\$?; echo DH_INGEST_EXIT=\$ec; exit \$ec"
  exit $?
fi

# ── 宿主机模式（默认）────────────────────────────────────────────────────────
if [[ ! -f "$HOST_RECIPE" ]]; then
  echo "ERROR: recipe 不存在: $HOST_RECIPE" >&2
  exit 1
fi

export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
export PYTHONUNBUFFERED=1

if ! "$PYTHON" -c "import datahub" >/dev/null 2>&1; then
  echo "ERROR: 当前 Python 未安装 DataHub CLI 包: $PYTHON" >&2
  echo "  请执行: $PYTHON -m pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'" >&2
  exit 1
fi
if ! "$PYTHON" -c "import datahub_classify" >/dev/null 2>&1; then
  echo "ERROR: 缺少 datahub_classify（hive-metastore 源在 1.5.x 下会导入 classification 依赖）: $PYTHON" >&2
  echo "  请执行: $PYTHON -m pip install -U 'acryl-datahub[presto-on-hive]'" >&2
  echo "  或一次装全: $PYTHON -m pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'" >&2
  exit 1
fi

echo "[INFO] hive_metastore data_finance ingest (host) PYTHON=$PYTHON recipe=$HOST_RECIPE DATAHUB_GMS_URL=$DATAHUB_GMS_URL"

set +e
"$PYTHON" -m datahub ingest -c "$HOST_RECIPE"
ec=$?
set -e
echo "DH_INGEST_EXIT=$ec"
exit "$ec"
