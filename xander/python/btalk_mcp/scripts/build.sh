#!/usr/bin/env bash
# build.sh - 构建 btalk-mcp:enhanced 镜像
#
# 用法:
#   ./scripts/build.sh                       # 用默认版本(取 Dockerfile ARG 默认值)
#   ./scripts/build.sh 0.4.7                 # 指定 btalk-cli 版本
#   BTALK_CLI_VERSION=0.4.7 ./scripts/build.sh
#
# 输出:
#   btalk-mcp:enhanced                 指向最新构建
#   btalk-mcp:enhanced-<cli>-<date>    不可变快照,用于回滚

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

BTALK_CLI_VERSION="${1:-${BTALK_CLI_VERSION:-}}"
DATE_TAG="$(date +%Y%m%d)"
PLATFORM="${PLATFORM:-linux/amd64}"

if [ -z "$BTALK_CLI_VERSION" ]; then
  # 取 Dockerfile 默认 ARG
  BTALK_CLI_VERSION="$(grep -E '^ARG[[:space:]]+BTALK_CLI_VERSION=' Dockerfile | head -1 | sed -E 's/.*=([0-9.]+).*/\1/')"
fi

if [ -z "$BTALK_CLI_VERSION" ]; then
  echo "ERROR: 没法确定 btalk-cli 版本,显式传入或设置 BTALK_CLI_VERSION" >&2
  exit 2
fi

IMMUTABLE_TAG="btalk-mcp:enhanced-${BTALK_CLI_VERSION}-${DATE_TAG}"

echo "==> 构建 $IMMUTABLE_TAG (platform=${PLATFORM}, 同时打 :enhanced 最新标签)"
docker build \
  --platform "$PLATFORM" \
  --build-arg "BTALK_CLI_VERSION=${BTALK_CLI_VERSION}" \
  -t "$IMMUTABLE_TAG" \
  -t btalk-mcp:enhanced \
  -f Dockerfile \
  "$ROOT_DIR"

echo
echo "==> 镜像构建完成:"
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}' \
  | grep -E '^btalk-mcp:(enhanced|enhanced-)' | head -5
echo
echo "==> 下一步: ./scripts/deploy.sh ${IMMUTABLE_TAG}"
