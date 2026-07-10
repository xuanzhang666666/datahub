# syntax=docker/dockerfile:1.6
# btalk-mcp: enhanced MCP server wrapping @wnpm/btalk-cli.
#
# Base layout: 源镜像装好 @wnpm/btalk-cli 与 supergateway,
# 源镜像的 src/mcp/tools.js 上游只注册 6 个 SDK 直连工具,
# 本文件用 build context 里的 src/mcp/tools.js 覆盖掉,暴露 23 个工具
# (SDK 直连 + btalk CLI 子命令桥接)。
#
# Build:  docker build -t btalk-mcp:enhanced .
# 升级: 改 BTALK_CLI_VERSION 后重新 build;若 @wnpm/btalk-cli SDK 行为变了,
#        同步更新 src/mcp/tools.js(zod schema、handler 参数)。
ARG BTALK_CLI_VERSION=0.4.6
ARG NODE_VERSION=22.23.1

FROM node:${NODE_VERSION}-slim

ARG BTALK_CLI_VERSION
RUN npm install -g "@wnpm/btalk-cli@${BTALK_CLI_VERSION}" \
        --registry=https://registry.corp.bianlifeng.com \
    && npm install -g supergateway

# 增强版 tools.js:把 btalk CLI 子命令(ripple/user/group/pc_helper/...)包装成 MCP tools
COPY src/mcp/tools.js /usr/local/lib/node_modules/@wnpm/btalk-cli/src/mcp/tools.js
COPY src/mcp/daemon_handlers.js /usr/local/lib/node_modules/@wnpm/btalk-cli/src/mcp/daemon_handlers.js

ENV BTALK_MCP_PORT=9013
EXPOSE 9013

# 启动顺序: 后台拉起 btalkd 守护进程,等 8s 让其登录并建好 socket,
# 然后 supergateway 把 stdio 上的 btalk-mcp 暴露为 streamable HTTP。
CMD ["sh", "-c", "node /usr/local/bin/btalkd & echo 'waiting for btalkd...' && sleep 8 && supergateway --outputTransport streamableHttp --stateful --port ${BTALK_MCP_PORT} --streamableHttpPath /mcp --stdio 'btalk-mcp'"]
