#!/usr/bin/env bash
# Jenkins parameters:
#   ACTION=apply|dry-run|restore
#   JOBS=<Multi-line String, one job per line>
# Restore:
#   BACKUP_RUN_DIR=/data/datahub/backups/jenkins-retry/<run_id>
#   RESTORE_ALL=1  # required only when JOBS is empty
set -euo pipefail
umask 077

set -a
source /data/datahub/scripts/lineage.env
set +a

export ACTION="${ACTION:-dry-run}"
export BLF_JENKINS_URL="${BLF_JENKINS_URL:-http://schedule.corp.bianlifeng.com}"
if [[ -z "${BLF_JENKINS_TOKEN:-}" && -n "${BLF_JENKINS_PASSWORD:-}" ]]; then
    export BLF_JENKINS_TOKEN="$BLF_JENKINS_PASSWORD"
fi

echo "[INFO] Jenkins Naginator batch action: $ACTION"
echo "[INFO] Jenkins URL: $BLF_JENKINS_URL"
echo "[INFO] Backup root: ${JENKINS_RETRY_BACKUP_ROOT:-/data/datahub/backups/jenkins-retry}"

exec "${LINEAGE_PYTHON:-/opt/anaconda3/bin/python}" \
    /data/datahub/scripts/update_jenkins_retry_config.py "$@"
