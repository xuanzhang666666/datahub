#!/usr/bin/env sh
# Jenkins: Hive Metastore → DataHub ingest (typically daily).
#
# Deployment model (BLF):
#   - neo4j2.dp.data.bj1 is a Jenkins slave → jobs run ON that host: use mode **local**
#     (default). No agent-bastion in Jenkins.
#   - agent-bastion + put2/get2 is only for **laptop → neo4j2** to deploy recipe/scripts; Jenkins
#     does not need it once the slave runs on neo4j2.
#
# 默认走宿主机 ingest（由 run_hive_*_on_neo4j2.sh 内 python -m datahub，不经 Docker），与血缘任务
# 共用 LINEAGE_PYTHON=/opt/anaconda3/bin/python 等约定。若未设置，本脚本在 local 模式下会给出
# 与 neo4j2 常见部署一致的默认值（仍可在 Jenkins Job 里覆盖）。
#
# Prerequisites on neo4j2 (once):
#   - /data/datahub/recipes/hive_metastore_data_multi_dbs.yml（或 finance 专用 recipe）
#   - /data/datahub/scripts/run_hive_data_multi_dbs_on_neo4j2.sh（及 finance 脚本），chmod +x
#   - Anaconda: /opt/anaconda3/bin/python -m pip install -U 'acryl-datahub[hive-metastore,presto-on-hive]'
#   - Optional: copy this file to /data/datahub/scripts/jenkins_hive_metastore_ingest.sh
#
# Jenkins Freestyle (slave = neo4j2) — "Execute shell":
#   export LINEAGE_PYTHON=/opt/anaconda3/bin/python
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080
#   sh /data/datahub/scripts/jenkins_hive_metastore_ingest.sh
#   # data_finance only: set JENKINS_HIVE_REMOTE_SCRIPT=/data/datahub/scripts/run_hive_data_finance_on_neo4j2.sh
#
# Optional overrides:
#   JENKINS_HIVE_INGEST_MODE       default local (bastion only for remote-from-laptop)
#   JENKINS_HIVE_REMOTE_SCRIPT     default /data/datahub/scripts/run_hive_data_multi_dbs_on_neo4j2.sh
#   LINEAGE_PYTHON / DATAHUB_GMS_URL — 见上；未设置时 local 模式默认 /opt/anaconda3/bin/python 与 http://127.0.0.1:8080
#   HIVE_INGEST_USE_DOCKER=1 — 传给 run_hive_*，强制回退到 docker exec 旧路径
#   JENKINS_BASTION_* / JENKINS_NEO4J2_TARGET / JENKINS_SSH_EXTRA_OPTS — only when MODE=bastion
#   TZ — for log() timestamps (default Asia/Shanghai)
set -eu

MODE="${JENKINS_HIVE_INGEST_MODE:-local}"
REMOTE_SCRIPT="${JENKINS_HIVE_REMOTE_SCRIPT:-/data/datahub/scripts/run_hive_data_multi_dbs_on_neo4j2.sh}"
BASTION_USER="${JENKINS_BASTION_USER:-agent}"
BASTION_HOST="${JENKINS_BASTION_HOST:-10.253.40.11}"
BASTION_PORT="${JENKINS_BASTION_PORT:-7233}"
BASTION_KEY="${JENKINS_BASTION_KEY:-$HOME/.ssh/agent-bastion}"
NEO4J2_TARGET="${JENKINS_NEO4J2_TARGET:-neo4j2.dp.data.bj1}"
EXTRA_OPTS="${JENKINS_SSH_EXTRA_OPTS:-}"

log() {
  printf '%s %s\n' "$(TZ="${TZ:-Asia/Shanghai}" date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

log "jenkins_hive_metastore_ingest MODE=$MODE REMOTE_SCRIPT=$REMOTE_SCRIPT"

case "$MODE" in
  local)
    if ! test -f "$REMOTE_SCRIPT"; then
      log "ERROR: not found: $REMOTE_SCRIPT"
      exit 1
    fi
    # 与 Jenkins 血缘任务对齐的默认值（已在 Job 中 export 则不会覆盖）
    LINEAGE_PYTHON="${LINEAGE_PYTHON:-/opt/anaconda3/bin/python}"
    export LINEAGE_PYTHON
    DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
    export DATAHUB_GMS_URL
    log "defaults if unset: LINEAGE_PYTHON=$LINEAGE_PYTHON DATAHUB_GMS_URL=$DATAHUB_GMS_URL"
    if command -v bash >/dev/null 2>&1; then
      exec bash "$REMOTE_SCRIPT"
    fi
    log "ERROR: bash is required to run $REMOTE_SCRIPT (run_hive_* use bash; install bash or invoke this wrapper with bash)."
    exit 1
    ;;
  bastion)
    if ! test -f "$BASTION_KEY"; then
      log "ERROR: bastion key file missing: $BASTION_KEY (set JENKINS_BASTION_KEY)"
      exit 1
    fi
    if ! test -n "$NEO4J2_TARGET"; then
      log "ERROR: JENKINS_NEO4J2_TARGET is empty (e.g. neo4j2.dp.data.bj1)"
      exit 1
    fi
    REMOTE_CMD="@${NEO4J2_TARGET} ${REMOTE_SCRIPT}"
    log "ssh ${BASTION_USER}@${BASTION_HOST}:${BASTION_PORT} -> ${REMOTE_CMD}"
    exec ssh -i "$BASTION_KEY" -p "$BASTION_PORT" -o StrictHostKeyChecking=no \
      $EXTRA_OPTS \
      "${BASTION_USER}@${BASTION_HOST}" \
      "$REMOTE_CMD"
    ;;
  *)
    log "ERROR: unknown JENKINS_HIVE_INGEST_MODE=$MODE (use local or bastion)"
    exit 1
    ;;
esac
