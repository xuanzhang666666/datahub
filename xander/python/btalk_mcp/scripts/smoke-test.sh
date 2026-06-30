#!/usr/bin/env bash
# smoke-test.sh - 验证 btalk-mcp 服务端到端是否健康
#
# 检查项:
#   1. /mcp initialize 通过
#   2. tools/list 数量 >= 24(基线,低于 24 即视为回退)
#   3. 抽样调 6 个工具,每个都返回 ok=true

set -euo pipefail

MCP_URL="${MCP_URL:-http://neo4j2.dp.data.bj1.wormpex.com:9013/mcp}"

if [ "${1:-}" = "--check-only" ]; then
  CHECK_ONLY=1
else
  CHECK_ONLY=0
fi

PY="$(command -v python3)"

cat > /tmp/btalk_mcp_smoke.py <<'PY'
import os, sys, json, re, urllib.request

URL = os.environ.get('MCP_URL', 'http://neo4j2.dp.data.bj1.wormpex.com:9013/mcp')
HEADERS = {'Content-Type': 'application/json',
           'Accept': 'application/json, text/event-stream'}
session_id = None

def post(payload, timeout=30):
    global session_id
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                 headers={**HEADERS, **({'mcp-session-id': session_id} if session_id else {})},
                                 method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        session_id = r.headers.get('mcp-session-id') or session_id
        text = r.read().decode()
    m = re.search(r'^data: (.*)$', text, re.M)
    return json.loads(m.group(1)) if m else None

# 1. initialize
init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "smoke", "version": "1"}}})
assert init and 'result' in init, f"initialize failed: {init}"
post({"jsonrpc": "2.0", "method": "notifications/initialized"})

# 2. tools/list
tl = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = tl['result']['tools']
print(f"tool_count: {len(tools)}")
if len(tools) < 24:
    print(f"FAIL: 工具数 {len(tools)} < 24 基线,疑似回退到默认 btalk-mcp", file=sys.stderr)
    sys.exit(2)

# 3. 抽样调用
CASES = [
    ('btalk_status', {}),
    ('btalk_version', {}),
    ('user_lookup', {'action': 'me'}),
    ('otp', {}),
    ('ripple_category', {'tab': 1}),
    ('ripple_list', {'tab': 1, 'page': 1, 'size': 3}),
]
fail = 0
for name, args in CASES:
    r = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
    ok = r and 'result' in r and not r['result'].get('isError')
    snippet = ''
    if r and 'result' in r:
        for c in r['result'].get('content', []):
            snippet = c.get('text', '')[:120]
    print(f"  {'OK ' if ok else 'FAIL'} {name:20s} {snippet}")
    if not ok:
        fail += 1
sys.exit(1 if fail else 0)
PY

MCP_URL="$MCP_URL" "$PY" /tmp/btalk_mcp_smoke.py
