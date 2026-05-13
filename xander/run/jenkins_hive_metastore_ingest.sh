#!/usr/bin/env sh
# Jenkins: Hive Metastore → DataHub ingest (typically daily).
#
# Deployment model (BLF):
#   - neo4j2.dp.data.bj1 is a Jenkins slave → jobs run ON that host: use mode **local**
#     (default). No agent-bastion in Jenkins.
#   - agent-bastion + put2/get2 is only for **laptop → neo4j2** to deploy recipe/scripts; Jenkins
#     does not need it once the slave runs on neo4j2.
#
# Prerequisites on neo4j2 (once, via your usual FTP/bastion deploy):
#   - /data/datahub/scripts/run_hive_data_multi_dbs_on_neo4j2.sh (+ recipe yml), chmod +x
#   - data_finance only: deploy hive_metastore_data_finance.yml to /data/datahub/recipes/ and
#     run_hive_data_finance_on_neo4j2.sh to /data/datahub/scripts/; use a second Jenkins job with
#     JENKINS_HIVE_REMOTE_SCRIPT=/data/datahub/scripts/run_hive_data_finance_on_neo4j2.sh
#   - Optional: copy this file to /data/datahub/scripts/jenkins_hive_metastore_ingest.sh so
#     Jenkins can run it without checking out the Git repo.
#
# Jenkins Freestyle (slave = neo4j2.dp.data.bj1, label e.g. neo4j2_bj1-a5fb290e) — "Execute shell":
#   bash /data/datahub/scripts/jenkins_hive_metastore_ingest.sh
#   (sh is fine for this wrapper; it execs bash for run_hive_*.)
#   If docker.sock permission denied: add agent user to group docker, OR configure NOPASSWD
#   sudo for docker and export DATAHUB_DOCKER_SUDO=1 before this script (see run_hive_* header).
#   # or, if the job checks out this repo on the same slave:
#   bash "$WORKSPACE/xander/run/jenkins_hive_metastore_ingest.sh"
#
# Optional: from laptop only — trigger ingest through bastion (not for Jenkins slave):
#   export JENKINS_HIVE_INGEST_MODE=bastion
#   export JENKINS_BASTION_KEY="$HOME/.ssh/agent-bastion"
#   export JENKINS_NEO4J2_TARGET=neo4j2.dp.data.bj1
#   bash xander/run/jenkins_hive_metastore_ingest.sh
#
# Optional overrides:
#   JENKINS_HIVE_INGEST_MODE       default local (bastion only for remote-from-laptop)
#   JENKINS_HIVE_REMOTE_SCRIPT     default /data/datahub/scripts/run_hive_data_multi_dbs_on_neo4j2.sh
#   JENKINS_BASTION_* / JENKINS_NEO4J2_TARGET / JENKINS_SSH_EXTRA_OPTS — only when MODE=bastion
#   DATAHUB_DOCKER_SUDO — passed through to run_hive_* (auto|0|1); auto uses sg docker if Jenkins
#     process was not restarted after usermod -aG docker.
#   TZ — for log() timestamps (default Asia/Shanghai); Python ingest time uses run_hive_* docker -e TZ.
#
# HMS / GMS: see run_hive_*_on_neo4j2.sh defaults on neo4j2; override there or via a wrapper.
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
