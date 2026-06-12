#!/usr/bin/env bash
# Run the BLF Jenkins schedule MCP server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="${BLF_SCHEDULE_MCP_ENV_FILE:-/data/datahub/scripts/lineage.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ -x "$HOME/anaconda3/bin/python" ]]; then
  PYTHON="$HOME/anaconda3/bin/python"
else
  PYTHON=python3
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export BLF_JENKINS_URL="${BLF_JENKINS_URL:-http://schedule.corp.bianlifeng.com}"
export BLF_SCHEDULE_MCP_HOST="${BLF_SCHEDULE_MCP_HOST:-0.0.0.0}"
export BLF_SCHEDULE_MCP_PORT="${BLF_SCHEDULE_MCP_PORT:-9012}"
if [[ -z "${BLF_JENKINS_TOKEN:-}" && -n "${BLF_JENKINS_PASSWORD:-}" ]]; then
  export BLF_JENKINS_TOKEN="$BLF_JENKINS_PASSWORD"
fi

if [[ -z "${BLF_JENKINS_USER:-}" ]]; then
  echo "ERROR: BLF_JENKINS_USER is required." >&2
  exit 1
fi
if [[ -z "${BLF_JENKINS_TOKEN:-}" ]]; then
  echo "ERROR: BLF_JENKINS_TOKEN or BLF_JENKINS_PASSWORD is required." >&2
  exit 1
fi
if [[ -z "${BLF_SCHEDULE_MCP_TOKEN:-}" ]]; then
  echo "ERROR: BLF_SCHEDULE_MCP_TOKEN is required." >&2
  exit 1
fi

exec "$PYTHON" -m blf_schedule_mcp.server \
  --host "$BLF_SCHEDULE_MCP_HOST" \
  --port "$BLF_SCHEDULE_MCP_PORT"
