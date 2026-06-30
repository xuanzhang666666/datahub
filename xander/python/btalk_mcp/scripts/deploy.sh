#!/usr/bin/env bash
# deploy.sh - 把 btalk-mcp 镜像部署到 neo4j2
#
# 用法:
#   ./scripts/deploy.sh                              # 用 :enhanced 最新
#   ./scripts/deploy.sh btalk-mcp:enhanced-0.4.6-20260701  # 指定镜像
#   ./scripts/deploy.sh --rollback                    # 回滚到上一个 :enhanced-<cli>-<date> 镜像
#
# 行为:
#   1. ssh 到 neo4j2 检查目标镜像是否存在
#   2. docker rm -f btalk-mcp(保留 /root/.btalk 登录态)
#   3. docker run -d --name btalk-mcp ... 启动新容器
#   4. 等 btalkd 登录、supergateway 就绪,跑 smoke-test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

BASTION_KEY="${AGENT_BASTION_KEY:-$HOME/.ssh/agent-bastion}"
BASTION_HOST="${BASTION_HOST:-10.253.40.11}"
BASTION_PORT="${BASTION_PORT:-7233}"
BASTION_USER="${BASTION_USER:-agent}"
NEO4J_HOST="neo4j2.dp.data.bj1"

run_remote() {
  ssh -i "$BASTION_KEY" -p "$BASTION_PORT" -o StrictHostKeyChecking=no \
      "$BASTION_USER@$BASTION_HOST" "@$NEO4J_HOST $*"
}

resolve_image() {
  local arg="${1:-}"
  if [ -z "$arg" ] || [ "$arg" = "--rollback" ]; then
    if [ "$arg" = "--rollback" ]; then
      # 取 :enhanced-* 镜像里 createdAt 倒数第二新的(当前 :enhanced 不算)
      run_remote "docker images --format '{{.Repository}}:{{.Tag}}  {{.CreatedAt}}' \
        | awk '/^btalk-mcp:enhanced-/ {print}' | sort -k2,2 | tail -1 | awk '{print \$1}'"
    else
      echo "btalk-mcp:enhanced"
    fi
  else
    echo "$arg"
  fi
}

IMAGE="$(resolve_image "${1:-}")"

echo "==> 目标镜像: $IMAGE"

# 1. 校验镜像在 neo4j2 上存在
echo "==> 检查镜像是否已存在"
run_remote "docker image inspect '$IMAGE' --format '{{.Id}} ({{.Created}})'" \
  || { echo "ERROR: 镜像 $IMAGE 在 neo4j2 上不存在,先 build" >&2; exit 1; }

# 2. 停旧容器(保留 /root/.btalk 登录态)
echo "==> 停旧容器"
run_remote "docker rm -f btalk-mcp 2>/dev/null || true"

# 3. 启动新容器
echo "==> 启动新容器"
run_remote "docker run -d --name btalk-mcp --restart unless-stopped \
  -p 9013:9013 -v /root/.btalk:/root/.btalk '$IMAGE'"

# 4. 等就绪
echo "==> 等 btalkd 登录 + supergateway 就绪(最多 30s)"
for i in $(seq 1 30); do
  if curl -fs -X POST http://neo4j2.dp.data.bj1.wormpex.com:9013/mcp \
       -H "Content-Type: application/json" \
       -H "Accept: application/json, text/event-stream" \
       -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"deploy-check","version":"0"}}}' \
       > /dev/null 2>&1; then
    echo "    ready after ${i}s"
    break
  fi
  sleep 1
  if [ "$i" = "30" ]; then
    echo "ERROR: 30s 内 9013 没就绪" >&2
    run_remote "docker logs --tail 50 btalk-mcp" >&2
    exit 1
  fi
done

# 5. 跑 smoke test
echo "==> 跑 smoke test"
"$ROOT_DIR/scripts/smoke-test.sh"

echo
echo "==> 部署完成: $IMAGE"
