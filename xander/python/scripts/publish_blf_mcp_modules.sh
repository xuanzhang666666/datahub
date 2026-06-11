#!/usr/bin/env bash
# Local: package BLF MCP modules and put2 flat tarball + deploy script to FTP.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="${TMPDIR:-/tmp}/blf_mcp_ftp_stage.$$"
PAYLOAD="$STAGE/payload"
mkdir -p "$PAYLOAD"
trap 'rm -rf "$STAGE"' EXIT

cp -R "$ROOT/blf_datahub_mcp" "$ROOT/blf_trino_mcp" "$PAYLOAD/"
cp "$ROOT/scripts/run_blf_datahub_mcp.sh" "$ROOT/scripts/run_blf_trino_mcp.sh" "$PAYLOAD/"
cp "$ROOT/scripts/deploy_blf_mcp_modules.sh" "$STAGE/deploy_blf_mcp_modules.sh"

TAR_NAME=blf_mcp_modules.tar.gz
tar -czf "$STAGE/$TAR_NAME" -C "$PAYLOAD" .

PUT2="${PUT2_SCRIPT:-$HOME/Documents/script/shell/put_local_2_ftp.sh}"
(
  cd "$STAGE"
  sh "$PUT2" "$TAR_NAME"
  sh "$PUT2" deploy_blf_mcp_modules.sh
)
echo "done; on neo4j2: get2 deploy_blf_mcp_modules.sh && bash /root/deploy_blf_mcp_modules.sh"
