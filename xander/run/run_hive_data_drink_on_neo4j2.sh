#!/usr/bin/env bash
# Run on neo4j2 (SSH/cron). HMS Thrift ingestion for database data_drink (all tables).
#
# Deploy (flat names for put2 FTP):
#   cp xander/recipes/hive_metastore_data_drink.yml ./hive_metastore_data_drink.yml
#   cp xander/run/run_hive_data_drink_on_neo4j2.sh ./run_hive_data_drink_on_neo4j2.sh
#   put2 hive_metastore_data_drink.yml
#   put2 run_hive_data_drink_on_neo4j2.sh
# On neo4j2 after get2: mv both → /data/datahub/scripts/ && chmod +x run_hive_data_drink_on_neo4j2.sh
#
# Environment:
#   HMS_THRIFT_HOST HMS_THRIFT_PORT DATAHUB_GMS_URL
#   DATAHUB_GMS_TOKEN optional; DATAHUB_ACTIONS_CONTAINER to pin container name
#   RUN_ON_HOST=1 to run datahub ingest on the host instead of docker exec
#   PYTHONUNBUFFERED=1 is set for docker/host so progress logs stream (e.g. Jenkins).
#   DATAHUB_DOCKER_SUDO: auto|0|1 — auto tries docker → sg docker → sudo -n docker (see multi_dbs script).
#   TZ — default Asia/Shanghai for Python logs inside docker exec / RUN_ON_HOST.
set -euo pipefail
SCRIPT_DIR=/data/datahub/scripts
RECIPE_NAME=hive_metastore_data_drink.yml
HOST_RECIPE="$SCRIPT_DIR/$RECIPE_NAME"
TMP_RECIPE="/tmp/$RECIPE_NAME"

export HMS_THRIFT_HOST="${HMS_THRIFT_HOST:-hiveserver5.dp.data.bj1.wormpex.com}"
export HMS_THRIFT_PORT="${HMS_THRIFT_PORT:-9083}"
export TZ="${TZ:-Asia/Shanghai}"

if [[ "${RUN_ON_HOST:-0}" == "1" ]]; then
  export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
  export PYTHONUNBUFFERED=1
  exec datahub ingest -c "$HOST_RECIPE"
fi

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
      echo "ERROR: cannot access Docker (e.g. permission denied on /var/run/docker.sock)." >&2
      echo "  Fix A: add the Jenkins agent user to the docker group; restart swarm agent OR use sg (auto)." >&2
      echo "  Fix B: NOPASSWD for docker in sudoers, then export DATAHUB_DOCKER_SUDO=1 in the Jenkins job." >&2
      echo "  Fix C: RUN_ON_HOST=1 if datahub CLI is installed on the host." >&2
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
  "$CTR" datahub ingest -c "$TMP_RECIPE"
