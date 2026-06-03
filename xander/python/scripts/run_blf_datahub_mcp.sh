#!/usr/bin/env bash
# Run the BLF DataHub Hive MCP server on neo4j2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="${BLF_DATAHUB_MCP_ENV_FILE:-/data/datahub/scripts/lineage.env}"
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
export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"
export DATAHUB_PUBLIC_BASE_URL="${DATAHUB_PUBLIC_BASE_URL:-http://neo4j2.dp.data.bj1.wormpex.com:9002}"
export BLF_DATAHUB_MCP_HOST="${BLF_DATAHUB_MCP_HOST:-0.0.0.0}"
export BLF_DATAHUB_MCP_PORT="${BLF_DATAHUB_MCP_PORT:-9010}"

if [[ -z "${DATAHUB_GMS_TOKEN:-}" ]]; then
  echo "ERROR: DATAHUB_GMS_TOKEN is required." >&2
  exit 1
fi
if [[ -z "${BLF_DATAHUB_MCP_TOKEN:-}" ]]; then
  echo "ERROR: BLF_DATAHUB_MCP_TOKEN is required." >&2
  exit 1
fi

exec "$PYTHON" -m blf_datahub_mcp.server \
  --host "$BLF_DATAHUB_MCP_HOST" \
  --port "$BLF_DATAHUB_MCP_PORT"

