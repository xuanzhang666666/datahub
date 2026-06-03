#!/usr/bin/env bash
# Run the BLF Trino Hive MCP server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="${BLF_TRINO_MCP_ENV_FILE:-/data/datahub/scripts/lineage.env}"
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
export TRINO_HOST="${TRINO_HOST:-10.253.7.167}"
export TRINO_PORT="${TRINO_PORT:-8081}"
export TRINO_USER="${TRINO_USER:-xuan.zhang}"
export TRINO_CATALOG="${TRINO_CATALOG:-hive}"
export TRINO_SCHEMA="${TRINO_SCHEMA:-default}"
export BLF_TRINO_MCP_HOST="${BLF_TRINO_MCP_HOST:-0.0.0.0}"
export BLF_TRINO_MCP_PORT="${BLF_TRINO_MCP_PORT:-9011}"

if [[ -z "${BLF_TRINO_MCP_TOKEN:-${BLF_DATAHUB_MCP_TOKEN:-}}" ]]; then
  echo "ERROR: BLF_TRINO_MCP_TOKEN or BLF_DATAHUB_MCP_TOKEN is required." >&2
  exit 1
fi

exec "$PYTHON" -m blf_trino_mcp.server \
  --host "$BLF_TRINO_MCP_HOST" \
  --port "$BLF_TRINO_MCP_PORT"
