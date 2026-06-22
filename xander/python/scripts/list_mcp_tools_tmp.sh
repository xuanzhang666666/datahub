#!/usr/bin/env bash
set -euo pipefail
. /data/datahub/scripts/lineage.env
for spec in "9010:$BLF_DATAHUB_MCP_TOKEN:datahub" "9011:$BLF_TRINO_MCP_TOKEN:trino" "9012:$BLF_SCHEDULE_MCP_TOKEN:schedule"; do
  IFS=: read -r port token label <<< "$spec"
  echo "=== $label ($port) ==="
  curl -sS -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
    "http://127.0.0.1:$port/mcp" | python3 - <<'PY'
import json,sys
payload=json.load(sys.stdin)
for tool in payload.get("result",{}).get("tools",[]):
    print(tool.get("name",""))
PY
done
