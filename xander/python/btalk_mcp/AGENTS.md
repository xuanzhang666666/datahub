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
| npm 包 | `@wnpm/btalk-cli@0.3.1`（内网 registry） |

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

## 工具列表（12 个）

| 工具名 | 功能 |
|--------|------|
| `list_conversations` | 列出所有会话（置顶+普通） |
| `search_contact` | 按子串搜索联系人/会话 |
| `fetch_history` | 拉取会话历史消息 |
| `send_message` | 发送文本消息 |
| `send_file` | 上传并发送文件/图片（蜂盘，100MB 上限） |
| `get_image_path` | 取图片/文件消息的本地缓存路径 |
| `btalk_status` | 查看 btalk daemon 登录与运行状态 |
| `wsso_cookie` | 获取或强制刷新公司内部系统 SSO Cookie |
| `ripple_list` | 查询 Ripple/蜂利器流程工单列表 |
| `ripple_show` | 查看 Ripple 工单详情 |
| `ripple_action` | 执行工单操作（领取/处理/反馈/转交） |
| `fetch_internal` | 用 wsso cookie 访问内部 HTTP 接口 |

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
4. ripple_action(id, op, fields={...})   # 实际提交
```

操作类型别名：`ACCEPT`/领取、`APPROVE`/处理、`FEEDBACK`/反馈、`ASSIGN`/转交

## 登录态

- `btalkd` 守护进程维护长连接，SSO Cookie 约 24h 自动续签（`SSO_STALE` → 自动重获）
- 登录态文件在 `/root/.btalk/`，挂载到宿主机，容器重建后不丢失
- 异常时用 `wsso_cookie(reset=true)` 强制刷新

## 注意事项

- 本目录是**源码镜像**，不是可直接运行的项目（缺少 node_modules 和 native `.node` 预编译库）
- 修改后需重新打包镜像并在 neo4j2 重建容器
- native 模块 `btalksdk-node.node` 需要 `libresolv.so.2`，镜像需基于带该库的 Linux 发行版
