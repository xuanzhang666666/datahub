#!/usr/bin/env bash
# Local: package blf_schedule_mcp + run/restart scripts and put2 to FTP.
# Mirror of publish_blf_mcp_modules.sh, but for the Jenkins schedule MCP.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="${TMPDIR:-/tmp}/blf_schedule_mcp_ftp_stage.$$"
PAYLOAD="$STAGE/payload"
mkdir -p "$PAYLOAD"
trap 'rm -rf "$STAGE"' EXIT

cp -R "$ROOT/blf_schedule_mcp" "$PAYLOAD/"
cp "$ROOT/scripts/run_blf_schedule_mcp.sh" "$PAYLOAD/"
cp "$ROOT/scripts/restart_blf_schedule_mcp.sh" "$PAYLOAD/"
cp "$ROOT/scripts/deploy_blf_schedule_mcp.sh" "$STAGE/deploy_blf_schedule_mcp.sh"

TAR_NAME=blf_schedule_mcp.tar.gz
tar -czf "$STAGE/$TAR_NAME" -C "$PAYLOAD" .

PUT2="${PUT2_SCRIPT:-$HOME/Documents/script/shell/put_local_2_ftp.sh}"
(
  cd "$STAGE"
  sh "$PUT2" "$TAR_NAME"
  sh "$PUT2" deploy_blf_schedule_mcp.sh
)
echo "done; on neo4j2: get2 deploy_blf_schedule_mcp.sh && bash /root/deploy_blf_schedule_mcp.sh"
