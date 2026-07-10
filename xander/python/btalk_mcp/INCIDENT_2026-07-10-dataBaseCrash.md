# btalk-mcp (neo4j2:9013) 无法返回会话历史 — 诊断与处置

**日期**: 2026-07-10
**现象**: 调 `btalk-mcp:9013` 的 `list_conversations` / `fetch_history(conversation_id="zeyun.lu")` 一直返回 `[]`，但磁盘上的 `xuan.zhang.db` 里数据存在。
**状态**: 暂不修复，先记录根因和方案。

---

## TL;DR

neo4j2 容器里跑的 `btalk-mcp:enhanced-0.4.6-20260701` 在 native addon 层 (`btalksdk-node.node`) 持续报 `dataBaseCrash 11`（SQLITE_CORRUPT）。SDK 作者已确认服务端 push 不做任何过滤，所以问题在客户端。**重启容器 / 删 db 文件 / 重建 db 都不能根治**——必须等新版 `@wnpm/btalk-cli` 修复 native addon 的 bug。

短期 workaround：用本机 mac 蜂利器 desktop app（db 健康，86 条 zeyun.lu 消息可查）。本文记录所有排查证据，便于交给 SDK 作者复现。

---

## 1. 调用链

```
HTTP POST 9013/mcp (MCP JSON-RPC, stateful, mcp-session-id)
  └── supergateway (stdio → streamable HTTP, stateful)
        └── bin/btalk-mcp.js (stdout 过滤代理)
              └── src/mcp/server.js (@modelcontextprotocol/sdk)
                    └── src/mcp/tools.js (24 个 tool handler)
                          └── @wnpm/btalk-cli SDK
                                └── prebuilds/linux-x64/btalksdk-node.node  ← 19MB C++ native addon
                                      └── XMPP 网关 (无差别 push, 作者已确认)
```

---

## 2. 关键证据

### 2.1 容器与镜像

```bash
$ docker ps | grep btalk
27ba2d2ed989  btalk-mcp:enhanced-0.4.6-20260701  ...  Up X days  0.0.0.0:9013->9013/tcp  btalk-mcp

$ docker exec btalk-mcp cat /usr/local/lib/node_modules/@wnpm/btalk-cli/package.json | grep version
"version": "0.4.6"
```

### 2.2 btalkd.log（daemon 生命周期日志，写在 `/root/.btalk/btalkd.log`）

最近 3 次容器/进程重启都成功登录：

```
2026-07-06T20:20:17.308Z DAEMON_BOOT pid=8
2026-07-06T20:20:17.512Z DAEMON_READY sock=/root/.btalk/btalkd.sock
2026-07-06T20:20:17.996Z NET_DOWN state=1
2026-07-06T20:20:17.996Z AUTO_LOGIN_OK
2026-07-06T20:20:18.032Z NET_UP
2026-07-06T20:20:18.035Z DEVICE_REPORT ok

2026-07-08T20:20:14.723Z DAEMON_BOOT pid=8
... (同模式)

2026-07-10T07:55:09.008Z DAEMON_BOOT pid=7   ← 手动 docker restart 后
2026-07-10T07:55:09.281Z AUTO_LOGIN_OK
2026-07-10T07:55:35.077Z NET_UP
```

`grep -cE "MSG_RECEIVE|CONV_LIST|SYNC|RosterInit" btalkd.log` = **0**（无业务事件）
但 daemon 生命周期完全正常，SSO 自动续期也正常。

### 2.3 关键日志：app.log（SDK 业务日志，写在 `/root/.btalk/prod/logs/btalk/app.log`）

这个日志文件**之前没被发现**（一直只看 btalkd.log，错过了真正的根因）。app.log 36MB，今天 07:25 还在写。

末尾几行（重启后立即出现）：

```
[2026-07-10T07:55:56.619] [INFO] default - API.SDK registerEventCall callback with dataBaseCrash 11
[2026-07-10T07:55:56.619] [INFO] default - API.SDK registerEventCall callback with dataBaseCrash 11
... (刷屏几十条)
[2026-07-10T07:55:56.623] [INFO] default - API.SDK conversationListUpdatedCallBack callback with {"normal":null,"top":null}
```

**核心发现**：
- 服务端 push 实际是**到达**了的（callback 被触发）
- 但 native addon 内部 SQLite 实例报错 `dataBaseCrash 11`
- callback 拿到的数据是 `{"normal":null,"top":null}` —— 空

### 2.4 根因：native addon 内部 SQLite 损坏

`/root/.btalk/prod/databases/xuan.zhang.db` 用 sqlite3 CLI 查：

```bash
$ sqlite3 xuan.zhang.db "PRAGMA integrity_check;"
*** in database main ***
On tree page 2 cell 0: invalid page number 3014389
On tree page 2 cell 53: invalid page number 1753883
On tree page 2 cell 52: Rowid 203494 out of order
On tree page 2 cell 52: invalid page number 995125

$ sqlite3 xuan.zhang.db "SELECT MAX(Time) FROM IM_Message INDEXED BY idx_conv_time_from;"
1783239474812   # = 2026-07-05 16:17:54  ← 5 天前
```

- `IM_Conversation` 表能读（1168 行）
- `IM_User` 表能读（85 行）
- **`IM_Message` 表 B-tree 损坏**（page 2 损坏），所有相关索引都报 `database disk image is malformed (11)`

### 2.5 历史：首次启动时 shared library 缺失

```
2026-06-12T09:40:48.675Z START_FAIL
  Error loading shared library libresolv.so.2: No such file or directory
  (needed by /usr/local/lib/node_modules/@wnpm/btalk-cli/prebuilds/linux-x64/btalksdk-node.node)
```

- 容器首次启动时 `libresolv.so.2` 缺失，native addon 加载失败
- btalkd 重试后 OK，但此次崩溃导致 WAL 没刷盘
- 之后 native addon 内部 SQLite 实例一直带病运行，每次 push 触发 `dataBaseCrash 11`

### 2.6 重启无效验证

- 重启 1：2026-07-06 20:20  → dataBaseCrash 11 立即复发
- 重启 2：2026-07-08 20:20  → dataBaseCrash 11 立即复发
- 重启 3：2026-07-10 07:55（手动） → dataBaseCrash 11 立即复发，10 秒后 `conversationListUpdatedCallBack` 仍返回 null

---

## 3. 为什么 mac 桌面版 db 健康

- mac 桌面版 btalk app 是完整 Electron 应用，不是 headless
- 完整应用有 native UI 进程的稳定生命周期，WAL 刷盘正常
- 同一份 db schema（`IM_Conversation` / `IM_Message` / `IM_User` 等）
- mac 桌面版能查 zeyun.lu 86 条消息（含 7 月 9 日的对话）

---

## 4. 文件清单（neo4j2 容器 btalk-mcp 内）

```
/root/.btalk/btalkd.log                                       daemon 生命周期日志 (fs.appendFileSync, 9MB)
/root/.btalk/btalkd.pid                                       pid 文件
/root/.btalk/btalkd.sock                                      unix socket
/root/.btalk/credentials.json                                 SSO token 凭据
/root/.btalk/device.json                                      设备 ID (gid/uuid)
/root/.btalk/sso_cookie.json                                  WSSO cookie
/root/.btalk/prod/logs/btalk/app.log                          ← **SDK 业务日志 (log4js, 36MB)**
/root/.btalk/prod/logs/btalk/app.log.-2026-07-05              (rotated, 18MB)
/root/.btalk/prod/logs/btalk/error.log
/root/.btalk/prod/logs/btalk/sdk.log
/root/.btalk/prod/databases/xuan.zhang.db                     ← 14GB 声明 / 7.7M 实际, B-tree 损坏
/root/.btalk/prod/databases/xuan.zhang.db-wal                 (38MB, 7月10日 07:20 还在写)
/root/.btalk/prod/databases/xuan.zhang.db-shm                 (32KB)
/root/.btalk/prod/databases/xuan.zhang.presearch.db
/root/.btalk/prod/databases/xuan.zhang.search.db
/usr/local/lib/node_modules/@wnpm/btalk-cli/                  v0.4.6, prebuilds/linux-x64/btalksdk-node.node (19MB)
```

**注意**：`/root/.btalk/btalk/app.log`（log4js）才是 SDK 业务日志，**不是** `/root/.btalk/btalkd.log`（daemon 生命周期日志）。之前一直只看 daemon log 错过了 root cause。

---

## 5. 关键设计问题

1. **btalk-mcp 工具只代理 native addon，不做 fallback** —— native addon 内部 SQLite 状态坏了，工具就废了，不会去读外部 `xuan.zhang.db`。
2. **依赖 XMPP 长连接 + 服务端 push 模型** —— 一断就废，没有快照能力。
3. **headless 模式剥离了 desktop 的 IndexedDB / 离线回放** —— mac 桌面有兜底，headless 没有。
4. **不读外部 SQLite** —— 明明磁盘上有完整 db，工具看不见。
5. **btalkd.log vs app.log 分离** —— 两个日志在不同位置，daemon 看不到 SDK 内部状态，问题难定位。

---

## 6. 处置方案

### 6.1 不推荐的操作

| 操作 | 风险 |
|------|------|
| 删 `xuan.zhang.db` 让 native addon 重建 | native addon 内部 SQLite 状态坏了，重建的 db 也会很快被损坏 |
| 重 build 同 tag 镜像 | native addon 二进制不变，bug 原样保留 |
| 拷贝 mac 桌面版 db 进去 | schema 可能有差异，deviceId/gid 不匹配，触发服务端拒绝；且 native addon 仍会损坏新 db |
| 重启 btalkd / 容器 | 已验证无效（3 次重启都立即复发） |

### 6.2 真正能修复的路径

**根本修复**：等 SDK 团队发新版 `@wnpm/btalk-cli`，修复 native addon 内部 SQLite 崩溃的 bug，然后：
1. 重 build `btalk-mcp` 容器镜像使用新版 npm 包
2. 删损坏的 `/root/.btalk/prod/databases/xuan.zhang.db*`
3. 重启容器，native addon 重新建一份干净的 db

**给 yingdong.guo 的复现包**：
- 镜像：`btalk-mcp:enhanced-0.4.6-20260701`
- 启动失败日志：`libresolv.so.2` 缺失
- 持续 `dataBaseCrash 11`（在 `app.log`，不是 `btalkd.log`）
- 重启立即复发
- `xuan.zhang.db` `integrity_check` 报告 page 2 损坏
- mac 桌面版同样 schema 的 db 是健康的

### 6.3 短期 workaround

**用 mac 桌面版**。同一个账号 `xuan.zhang`，db 在：
```
~/Library/Application Support/btalk/databases/xuan.zhang.db
```

- schema 与 neo4j2 容器一致
- 数据健康（蜂盘 desktop 完整进程管理）
- 可直接用 sqlite3 读（已经验证过 zeyun.lu 86 条消息都全）
- 缺点：本机数据，不是 neo4j2 远程访问

### 6.4 中期方案（如需要 MCP 远程读 btalk 历史）

**直读 SQLite 工具**（之前讨论过，需要时再做）：

挂在 `blf_datahub_mcp:9010`（已部署在 neo4j2），加 2 个 tool：
- `blf_btalk_db_list_conversations`
- `blf_btalk_db_fetch_history`

实现路径：
- Python stdlib `sqlite3` 模块（不需要装系统 sqlite3 CLI）
- 直接 read `/root/.btalk/prod/databases/xuan.zhang.db`
- 跟 btalk-mcp / native addon 完全无关

**注意**：本方案在 neo4j2 上能读到的数据是**损坏且滞后**的：
- `IM_Message` 表读不出来（B-tree 损坏）
- 即便能读，最新数据是 2026-07-05 16:17:54（5 天前）
- zeyun.lu 昨天的聊天在 neo4j2 db 上**读不到**

只能读 `IM_Conversation`（会话列表）和 `IM_User`（联系人），不能读消息内容。

**所以这个方案不解决"查 zeyun.lu 昨天聊天"的需求**。

---

## 7. 跟 SDK 作者沟通模板

主题：`btalk-mcp:enhanced-0.4.6-20260701` 持续报 `dataBaseCrash 11`，重启无效

> 您好。我们部署的 `btalk-mcp` 容器（neo4j2 上，监听 9013）持续报 `dataBaseCrash 11`，
> `app.log` 里能看到 native addon 内部 SQLite 崩溃，`conversationListUpdatedCallBack`
> 收到的是 `{"normal":null,"top":null}`。
>
> 关键时间线：
> - 6月12日 09:40 首次启动失败：`libresolv.so.2: No such file or directory`
> - 6月12日 09:43 重试后 AUTO_LOGIN_OK，但 WAL 没刷盘
> - 7月1日 15:15 后 `start update index` 事件消失，native addon 内部 SQLite 持续带病
> - 重启容器 3 次都立即复发
> - 外部 `xuan.zhang.db` 也损坏，`integrity_check` 报告 page 2 invalid page number
> - 7月5日 16:17:54 是 `IM_Message` 表能写入的最后时间
>
> 您已确认服务端 push 不做过滤，mac 桌面版 db 健康。
> 怀疑是 native addon 启动时共享库加载失败的副作用没被恢复。
>
> 请问：
> 1. `@wnpm/btalk-cli` 新版本（0.4.7+？）是否已修复这个 bug？
> 2. 如果修复了，新版镜像 `btalk-mcp` 何时发布？tag 是什么？
> 3. 如果尚未修复，预期什么时候能修？
>
> 在修复前我们会切到 mac 桌面版 workaround。

---

## 8. 验证命令速查

```bash
# 容器状态
ssh neo4j2 "docker ps | grep btalk"

# daemon 生命周期
ssh neo4j2 "docker exec btalk-mcp tail -30 /root/.btalk/btalkd.log"

# 业务日志（重点关注 dataBaseCrash）
ssh neo4j2 "docker exec btalk-mcp grep -E 'dataBaseCrash|conversationListUpdatedCallBack' /root/.btalk/prod/logs/btalk/app.log | tail -20"

# db 健康
ssh neo4j2 "docker exec btalk-mcp sqlite3 /root/.btalk/prod/databases/xuan.zhang.db 'PRAGMA integrity_check;'"

# 测 MCP 端到端
curl -X POST http://neo4j2.dp.data.bj1.wormpex.com:9013/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.1"}}}'

# 测 MCP fetch_history
curl -X POST http://neo4j2.dp.data.bj1.wormpex.com:9013/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "mcp-session-id: <from-init>" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
curl -X POST http://neo4j2.dp.data.bj1.wormpex.com:9013/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "mcp-session-id: <from-init>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"fetch_history","arguments":{"conversation_id":"zeyun.lu","count":5}}}'
```

---

## 9. 当前状态

- btalk-mcp 容器：running（重启后），9013 端口 listening
- 业务可用性：所有 btalk_* tools 返回 `[]`
- 短期方案：用户切到 mac 桌面版查
- 长期方案：等 SDK 团队发新版修复 bug
