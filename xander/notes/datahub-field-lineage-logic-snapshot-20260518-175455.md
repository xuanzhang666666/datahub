# DataHub 字段血缘加工逻辑阶段快照

本文记录 neo4j2 上 DataHub Docker Compose 部署在“字段级血缘 + 加工逻辑展示”阶段的冷快照信息。恢复步骤遵循 `xander/notes/datahub-snapshot-restore.md`。

## 快照信息

- 服务器：`neo4j2.dp.data.bj1`
- 部署目录：`/data/datahub`
- Compose 文件：`/data/datahub/docker-compose.yml`
- 快照时间：`2026-05-18 17:54:55`（UTC+8）
- 快照目录：`/data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/`
- 快照包：`/data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/datahub_root.tar`
- Compose 副本：`/data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/docker-compose.yml`
- 快照包大小：约 `11G`
- 校验和：

```text
a81737a2d8c3fca7742071efad5fe678  /data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/datahub_root.tar
```

## 快照目录内容

本次快照目录保留了以下文件，便于回滚前后核对：

- `datahub_root.tar`：`/data/datahub` 的冷快照包。
- `datahub_root.tar.md5`：快照包 MD5 校验和。
- `docker-compose.yml`：当前 Compose 配置副本。
- `docker-images.txt`：快照前宿主机 Docker 镜像列表。
- `compose-ps.before.txt` / `compose-ps.after.txt`：快照前后 Compose 状态。
- `df.before.txt` / `df.after.txt`：快照前后 `/data` 磁盘空间。
- `frontend-image.before.txt` / `frontend-image.after.txt`：快照前后前端容器镜像。
- `frontend-http.after.txt`：快照后前端 HTTP 检查结果。
- `gms-health.after.txt`：快照后 GMS health 检查输出。

## 当前镜像与服务状态

- GMS：`acryldata/datahub-gms:v1.5.0.4`
- Frontend：`datahub-frontend-react:lineage-sidebar-v1504-j17-cnlabel-20260518`
- Frontend 端口：`9002`
- GMS 端口：`8080`

快照后确认 Compose 已恢复运行，核心容器处于 healthy：

- `datahub-datahub-gms-1`
- `datahub-datahub-frontend-react-1`
- `datahub-mysql-1`
- `datahub-elasticsearch-1`
- `datahub-neo4j-1`
- `datahub-broker-1`
- `datahub-zookeeper-1`
- `datahub-schema-registry-1`

前端 `http://127.0.0.1:9002/` 返回 `HTTP/1.1 200 OK`。GMS health 端点请求成功，响应体为空。

## 本阶段包含的功能

本快照用于回滚到以下功能均已部署的状态：

1. 独立 Hive 字段级血缘导入链路，可基于审核后的 Excel 行写入 DataHub。
2. Excel 导入目标表时会替换该表旧的 fine-grained lineage，避免重复累积。
3. 字段血缘写入器会生成 Query entities，并关联 query URN，使 DataHub UI 可以展示 `LOGIC` / `transformOperation`。
4. 前端 lineage sidebar 支持在选中字段时打开，并展示字段加工逻辑。
5. Sidebar 标题已从 `Operation 1` 调整为 `字段加工逻辑 1`。
6. Sidebar 的 `INPUTS` / `OUTPUTS` 展示逻辑已调整为：`Tables` 显示表名，`Columns` 只显示列名。

相关本地代码提交包括 `59c1c1b37a feat(xander): show field lineage logic in sidebar` 以及当前分支上此前的字段血缘导入相关提交。

## 快速识别命令

在 neo4j2 上查看本快照：

```bash
ls -lh /data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/
md5sum /data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/datahub_root.tar
docker compose -f /data/datahub/docker-compose.yml ps
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:9002/
```

通过 Agent 跳板机执行示例：

```bash
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 "@neo4j2.dp.data.bj1 ls -lh /data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/"
```

## 回滚说明

恢复流程请优先参考 `xander/notes/datahub-snapshot-restore.md` 的“恢复流程”。本快照对应的解压命令为：

```bash
tar --numeric-owner -xf /data/datahub_snapshots/datahub_field_lineage_logic_20260518_175455/datahub_root.tar -C /data
```

回滚时注意：

- 恢复会替换当前 `/data/datahub`，执行前应先确认当前状态是否还需要留存。
- 建议先按参考文档将当前 `/data/datahub` 移动到 `datahub_broken_$(date +%Y%m%d_%H%M%S)` 之类的保留目录。
- 解压后使用 `/data/datahub/docker-compose.yml` 启动服务，并检查 GMS、Frontend、MySQL、Elasticsearch、Neo4j、Kafka、Zookeeper、Schema Registry 是否 healthy。
- 该快照包含 compose 配置、脚本、插件、数据目录以及可能的 token/signing key，应限制读取权限。
