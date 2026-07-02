#!/usr/bin/env bash
# Restart the BLF Jenkins schedule MCP server on neo4j2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/blf_schedule_mcp.pid"
LOG_FILE="$SCRIPT_DIR/blf_schedule_mcp.log"
RUN_SCRIPT="$SCRIPT_DIR/run_blf_schedule_mcp.sh"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" || true)"
else
  OLD_PID=""
fi

if [[ -z "$OLD_PID" ]]; then
  OLD_PID="$(ps -ef | awk '/[b]lf_schedule_mcp.server/ {print $2; exit}')"
fi

if [[ -n "$OLD_PID" ]]; then
  kill "$OLD_PID" || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! kill -0 "$OLD_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
fi

nohup "$RUN_SCRIPT" >>"$LOG_FILE" 2>&1 </dev/null &
NEW_PID="$!"
echo "$NEW_PID" >"$PID_FILE"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://localhost:9012/health" >/dev/null; then
    echo "BLF Schedule MCP restarted: pid=$NEW_PID"
    exit 0
  fi
  sleep 1
done

echo "ERROR: BLF Schedule MCP restart did not pass health check." >&2
tail -n 80 "$LOG_FILE" >&2 || true
exit 1
