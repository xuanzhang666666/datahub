#!/usr/bin/env bash
# Run on neo4j2 (SSH/cron). HMS Thrift ingestion for the multi-db recipe (see recipe YAML header).
#
# 默认在宿主机执行 ingest（与 run_batch_lineage_sync.sh / run_hive_data_finance_on_neo4j2.sh 一致），
# 不再依赖 docker exec 进 datahub-actions 容器。
#
# Deploy (flat names for put2 FTP):
#   cp xander/recipes/hive_metastore_data_multi_dbs.yml ./hive_metastore_data_multi_dbs.yml
#   cp xander/run/run_hive_data_multi_dbs_on_neo4j2.sh ./run_hive_data_multi_dbs_on_neo4j2.sh
#   put2 hive_metastore_data_multi_dbs.yml
#   put2 run_hive_data_multi_dbs_on_neo4j2.sh
# On neo4j2 after get2:
#   mv hive_metastore_data_multi_dbs.yml /data/datahub/recipes/
#   mv run_hive_data_multi_dbs_on_neo4j2.sh /data/datahub/scripts/ && chmod +x /data/datahub/scripts/run_hive_data_multi_dbs_on_neo4j2.sh
#
# Jenkins（经 jenkins_hive_metastore_ingest.sh）示例:
#   export LINEAGE_PYTHON=/opt/anaconda3/bin/python
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   sh /data/datahub/scripts/jenkins_hive_metastore_ingest.sh
#
# 宿主机依赖:
#   $LINEAGE_PYTHON -m pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'
#
# Environment:
#   HMS_THRIFT_HOST HMS_THRIFT_PORT DATAHUB_GMS_URL DATAHUB_GMS_TOKEN(optional)
#   LINEAGE_PYTHON / HIVE_INGEST_PYTHON / HIVE_INGEST_USE_DOCKER — 同 run_hive_data_finance_on_neo4j2.sh
#   DATAHUB_ACTIONS_CONTAINER / DATAHUB_DOCKER_SUDO — 仅 Docker 模式
#   TZ — default Asia/Shanghai
set -euo pipefail
RECIPE_DIR=/data/datahub/recipes
RECIPE_NAME=hive_metastore_data_multi_dbs.yml
HOST_RECIPE="$RECIPE_DIR/$RECIPE_NAME"
TMP_RECIPE="/tmp/$RECIPE_NAME"

export HMS_THRIFT_HOST="${HMS_THRIFT_HOST:-hiveserver5.dp.data.bj1.wormpex.com}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"
export TZ="${TZ:-Asia/Shanghai}"

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
  echo "ERROR: 缺少 datahub_classify: $PYTHON" >&2
  echo "  请执行: $PYTHON -m pip install -U 'acryl-datahub[presto-on-hive]'" >&2
  exit 1
fi

echo "[INFO] hive_metastore multi_dbs ingest (host) PYTHON=$PYTHON recipe=$HOST_RECIPE DATAHUB_GMS_URL=$DATAHUB_GMS_URL"

set +e
"$PYTHON" -m datahub ingest -c "$HOST_RECIPE"
ec=$?
set -e
echo "DH_INGEST_EXIT=$ec"
exit "$ec"
