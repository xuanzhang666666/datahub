#!/usr/bin/env bash
# neo4j2: get2 flat tarball from FTP, extract into /data/datahub/scripts/, restart MCP.
set -euo pipefail
DEST=/data/datahub/scripts
TAR_NAME=blf_mcp_modules.tar.gz

get2 "$TAR_NAME"
tar -xzf "/root/$TAR_NAME" -C "$DEST"
chmod +x "$DEST/run_blf_datahub_mcp.sh" "$DEST/run_blf_trino_mcp.sh"

restart_one() {
  local name="$1"
  local run_script="$2"
  local pid_file="$DEST/${name}.pid"
  local log_file="$DEST/${name}.log"
  if [[ -f "$pid_file" ]]; then
    old_pid="$(cat "$pid_file")"
    if kill -0 "$old_pid" 2>/dev/null; then
      kill "$old_pid"
      sleep 1
    fi
  fi
  (
    cd "$DEST"
    nohup sh "$run_script" >>"$log_file" 2>&1 &
    echo $! >"$pid_file"
  )
  echo "restarted $name pid=$(cat "$pid_file")"
}

restart_one blf_datahub_mcp run_blf_datahub_mcp.sh
restart_one blf_trino_mcp run_blf_trino_mcp.sh
echo "deployed BLF MCP modules to $DEST"
