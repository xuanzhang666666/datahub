#!/usr/bin/env bash
# neo4j2: get2 flat tarball from FTP, extract into /data/datahub/scripts/,
# then restart blf_schedule_mcp via the existing restart script.
set -euo pipefail
DEST=/data/datahub/scripts
TAR_NAME=blf_schedule_mcp.tar.gz

get2 "$TAR_NAME"
tar -xzf "/root/$TAR_NAME" -C "$DEST"
chmod +x "$DEST/run_blf_schedule_mcp.sh"

if [[ -f "$DEST/restart_blf_schedule_mcp.sh" ]]; then
  bash "$DEST/restart_blf_schedule_mcp.sh"
else
  echo "ERROR: $DEST/restart_blf_schedule_mcp.sh missing" >&2
  exit 1
fi
echo "deployed blf_schedule_mcp to $DEST"
