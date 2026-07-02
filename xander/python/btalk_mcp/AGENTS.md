# btalk_mcp — BLF 内部 IM (蜂利器) MCP Server 源码

本目录是从 neo4j2 Docker 容器（`btalk-mcp`）中提取的源码镜像，供 LLM 阅读和修改使用。
**生产服务运行在 neo4j2:9013（Docker 容器，streamable HTTP MCP）。**

## 部署信息

| 项目 | 值 |
|------|-----|
| 服务器 | neo4j2.dp.data.bj1 |
| 端口 | 9013 |
| 容器名 | btalk-mcp |
| 镜像 | btalk-mcp:enhanced |
| MCP 端点 | `http://neo4j2:9013/mcp` |
| 传输 | supergateway（stdio → streamable HTTP，stateful 模式） |
| 登录态 | `/root/.btalk/`（挂载到宿主机） |
| npm 包 | `@wnpm/btalk-cli@0.3.9`（内网 registry） |

## 架构

```
MCP 客户端 (HTTP POST /mcp)
  └── supergateway (stateful streamable HTTP)
        └── bin/btalk-mcp.js (stdout 过滤代理)
              └── src/mcp/server.js (MCP stdio server, @modelcontextprotocol/sdk)
                    └── src/mcp/tools.js (工具定义 + handler)
```

**stdout 过滤代理**：btalk native SDK 会往 stdout 打印调试信息，破坏 MCP 协议。
`bin/btalk-mcp.js` 用子进程隔离，只把 `{"jsonrpc":"2.0"...}` 行透传给父 stdout，其余重定向到 stderr。

**stateful 说明**：supergateway `--stateful` 模式下，每个 MCP session 结束后子进程正常回收重启，
日志里的 `Child exited: SIGTERM` 是正常行为，不是崩溃。

## 工具列表（23 个）

| 工具名 | 功能 |
|--------|------|
| `list_conversations` | 列出所有会话（置顶+普通） |
| `search_contact` | 按子串搜索联系人/会话 |
| `fetch_history` | 拉取会话历史消息 |
| `send_message` | 发送文本消息 |
| `send_file` | 上传并发送文件/图片（蜂盘，100MB 上限） |
| `get_image_path` | 取图片/文件消息的本地缓存路径 |
| `btalk_status` | 查看 btalk daemon 登录与运行状态 |
| `btalk_version` | 查看 btalk CLI 与 Node 版本 |
| `user_lookup` | 当前用户 / uid 查询 / 关键词搜索用户 |
| `group_manage` | 建群、拉人、踢人、群列表、详情、退群 |
| `otp` | 获取 6 位动态口令 |
| `wsso_cookie` | 获取或强制刷新公司内部系统 SSO Cookie |
| `ripple_list` | 查询 Ripple/蜂利器流程工单列表 |
| `ripple_category` | 查询 Ripple 流程类别统计 |
| `ripple_show` | 查看 Ripple 工单详情 |
| `ripple_short_url` | 生成 Ripple 工单短链 |
| `ripple_resolve` | 解析 Ripple 短链或 token |
| `ripple_group_chat` | 发起或进入 Ripple 工单群聊 |
| `ripple_flow_search` | 搜索流程模板，获取 flowCode |
| `ripple_create_order` | 按 flowCode 发起工单；默认保存草稿，submit 才提交 |
| `ripple_action` | 执行工单操作（领取/处理/反馈/转交），支持草稿 |
| `fetch_internal` | 用 wsso cookie 访问内部 HTTP 接口 |
| `pc_helper` | 个人电脑助手：给自己发消息、查状态、停止监听 |

## 关键文件

| 文件 | 说明 |
|------|------|
| `bin/btalk-mcp.js` | MCP 入口，stdout 过滤代理 |
| `src/mcp/server.js` | MCP stdio server（@modelcontextprotocol/sdk） |
| `src/mcp/tools.js` | 所有工具的 schema（zod）+ handler 实现 |
| `src/ripple.js` | Ripple 工单 API 完整实现（list/detail/action/submit） |
| `src/wsso.js` | SSO Cookie 获取与刷新 |
| `src/daemon.js` | btalkd 守护进程管理（IPC socket） |
| `src/index.js` | createBtalk / login / waitConnected 核心初始化 |
| `src/files.js` | 文件上传（蜂盘）实现 |
| `src/forward.js` | 内部 HTTP 请求转发（带 wsso cookie） |
| `src/headers.js` | 公司内部请求公共 header 构造 |
| `src/cli/commands.js` | CLI 辅助：flattenConversations / renderBody / formatTime |

## Ripple 工单操作流程

```
1. ripple_list(tab=1)                    # 查待处理工单，获得 flow_order_id
2. ripple_show(flow_order_id)            # 看详情 + operations（可用操作）
3. ripple_action(id, op, show_form=true) # 看表单字段填写规则（不提交）
4. ripple_action(id, op, draft=true, fields={...}) # 只存动作草稿，人工在桌面端审核
5. ripple_action(id, op, fields={...})   # 实际提交
```

操作类型别名：`ACCEPT`/领取、`APPROVE`/处理、`FEEDBACK`/反馈、`ASSIGN`/转交

## Ripple 发起工单流程

```
1. ripple_flow_search(keyword)                         # 查流程模板 flowCode
2. ripple_create_order(flow_code, show_form=true)      # 看发起表单字段/选项/级联
3. ripple_create_order(flow_code, fields={...})        # 默认只保存草稿
4. ripple_create_order(flow_code, fields={...}, submit=true) # 确认后真正提交
```

`ripple_create_order` 默认**保存草稿不提交**，适合 agent 先填表、人再在桌面端审核。

## 登录态

- `btalkd` 守护进程维护长连接，SSO Cookie 约 24h 自动续签（`SSO_STALE` → 自动重获）
- 登录态文件在 `/root/.btalk/`，挂载到宿主机，容器重建后不丢失
- 异常时用 `wsso_cookie(reset=true)` 强制刷新

## 构建 & 部署

| 文件 | 作用 |
|------|------|
| `Dockerfile` | 基于 `node:22.23.1-slim`，装 `@wnpm/btalk-cli` + `supergateway`，**用本目录 `src/mcp/tools.js` 覆盖上游的 6 工具版** |
| `scripts/build.sh` | `docker build -t btalk-mcp:enhanced[-<cli>-<date>]`；保留不可变标签用于回滚 |
| `scripts/deploy.sh` | ssh 跳板到 neo4j2，rm 旧容器 → run 新容器 → 等就绪 → 跑 smoke |
| `scripts/smoke-test.sh` | `initialize` + `tools/list` 计数 + 抽样调用 7 个工具；< 23 即视为回退；确认不暴露 `message_search` |
| `scripts/maintenance.py` | 清理可丢弃的 search/presearch 索引，避免 SQLite WAL 膨胀 |
| `scripts/healthcheck.py` | 检查 `btalk status` 登录态，异常时重启一次容器 |

## 运行时维护

neo4j2 上有两个 cron：

- `/etc/cron.d/btalk-mcp-healthcheck`: 每 10 分钟执行 `/root/btalk_mcp_healthcheck.py`，`loggedIn=true` 且 `state=ready` 才算健康；异常时 `docker restart btalk-mcp` 一次。
- `/etc/cron.d/btalk-mcp-maintenance`: 每天 04:20 执行 `/root/btalk_mcp_maintenance.py`，search/presearch 索引超过 512MB 时停容器、删除索引、再启动。

日志在 `/root/btalk_backups/healthcheck.log` 和 `/root/btalk_backups/maintenance.log`。

### 一次完整升级（CLI 新版本发布）

```bash
cd /Users/zhangxuan/Documents/wormpex/code-project/github/datahub/xander/python/btalk_mcp

# 1. 查 btalk-cli 最新版本
npm view @wnpm/btalk-cli version --registry=https://registry.corp.bianlifeng.com

# 2. 如有 SDK 行为变更(zod/SDK API),改 src/mcp/tools.js 适配

# 3. 升级
./scripts/build.sh 0.4.7        # 构建 btalk-mcp:enhanced-0.4.7-20260701
./scripts/deploy.sh              # 部署到 neo4j2,自动跑 smoke

# 4. 失败回滚
./scripts/deploy.sh --rollback   # 取上一个 btalk-mcp:enhanced-<cli>-<date> 镜像回滚
```

## 注意事项

- 本目录是**源码镜像**，`src/mcp/tools.js` 是相对于 npm 上游 6 工具的**增强补丁**。
  上游发布时只覆盖 6 个 SDK 直连工具，需要本目录的 `tools.js` 来补全 18 个 CLI 桥接工具。
- **必须用本目录的 `Dockerfile` 构建**，否则会用 npm 上游默认的 6 工具版。
- 改完 `tools.js` 后用 `./scripts/build.sh` 重新出镜像，再用 `./scripts/deploy.sh` 部署。
- native 模块 `btalksdk-node.node` 需要 `libresolv.so.2`，镜像需基于带该库的 Linux 发行版。
- 容器名固定 `btalk-mcp`，端口 `9013`，登录态卷 `/root/.btalk`，**重建容器时不要漏挂这个卷**。
