# DataHub 冷快照与恢复流程

本文记录 neo4j2 上 DataHub Docker Compose 部署的阶段性冷备份与恢复流程。适用于“表级血缘第一阶段完成后，需要保留当前系统与宿主机数据状态，便于后续出问题时整套回滚”的场景。

## 当前部署概况

- 服务器：`neo4j2.dp.data.bj1`
- 部署目录：`/data/datahub`
- Compose 文件：`/data/datahub/docker-compose.yml`
- GMS：`acryldata/datahub-gms:v1.5.0.4`，宿主机端口 `8080`
- Frontend：`acryldata/datahub-frontend-react:v1.5.0.4`，宿主机端口 `9002`
- 主要持久化目录：
  - `/data/datahub/mysql`
  - `/data/datahub/elasticsearch`
  - `/data/datahub/neo4j`
  - `/data/datahub/kafka`
  - `/data/datahub/zookeeper`
  - `/data/datahub/plugins`
  - `/data/datahub/recipes`
  - `/data/datahub/scripts`
  - `/data/datahub/in`
  - `/data/datahub/out`

## 本次快照信息

- 快照目录：`/data/datahub_snapshots/datahub_lineage_stage1_20260516_142043/`
- 快照包：`/data/datahub_snapshots/datahub_lineage_stage1_20260516_142043/datahub_root.tar`
- Compose 副本：`/data/datahub_snapshots/datahub_lineage_stage1_20260516_142043/docker-compose.yml`
- 快照包大小：约 `6.3G`
- 校验和：

```text
f5f290428adc7f5dd6f27610e20a5d53  /data/datahub_snapshots/datahub_lineage_stage1_20260516_142043/datahub_root.tar
```

## 为什么使用冷快照

MySQL、Elasticsearch、Neo4j、Kafka、Zookeeper 在服务运行时都会持续写入。直接在线打包数据目录可能得到不一致的文件系统状态，恢复后可能出现索引、事务日志或 broker 状态异常。

因此阶段性里程碑备份建议使用冷快照：

1. 先记录当前状态。
2. 停止 DataHub Compose。
3. 打包 `/data/datahub`。
4. 立即启动服务。
5. 验证服务恢复。

## 快照流程

### 1. 快照前检查

```bash
docker compose -f /data/datahub/docker-compose.yml ps
docker ps
df -h /data
curl http://127.0.0.1:8080/health
```

确认核心服务处于 healthy：

- `datahub-datahub-gms-1`
- `datahub-datahub-frontend-react-1`
- `datahub-mysql-1`
- `datahub-elasticsearch-1`
- `datahub-neo4j-1`
- `datahub-broker-1`
- `datahub-zookeeper-1`
- `datahub-schema-registry-1`

### 2. 创建快照目录

```bash
SNAP_NAME="datahub_lineage_stage1_$(date +%Y%m%d_%H%M%S)"
SNAP_DIR="/data/datahub_snapshots/${SNAP_NAME}"
mkdir -p "${SNAP_DIR}"
cp /data/datahub/docker-compose.yml "${SNAP_DIR}/docker-compose.yml"
docker image ls > "${SNAP_DIR}/docker-images.txt"
docker compose -f /data/datahub/docker-compose.yml ps > "${SNAP_DIR}/compose-ps.before.txt"
df -h /data > "${SNAP_DIR}/df.before.txt"
```

### 3. 停止 DataHub Compose

```bash
docker compose -f /data/datahub/docker-compose.yml down
```

注意：这会停止 DataHub Compose 项目内服务，不会停止独立运行的 `qdrant` 容器。

### 4. 打包数据目录

```bash
tar --numeric-owner -cf "${SNAP_DIR}/datahub_root.tar" -C /data datahub
```

使用 `--numeric-owner` 是为了保留 UID/GID，避免恢复后 MySQL、Elasticsearch、Neo4j 等目录权限不匹配。

### 5. 重新启动服务

```bash
docker compose -f /data/datahub/docker-compose.yml up -d
```

### 6. 启动后验证

```bash
docker compose -f /data/datahub/docker-compose.yml ps
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:9002/
df -h /data
```

确认：

- GMS 返回健康状态。
- Frontend 返回 DataHub 页面 HTML。
- GMS、Frontend、MySQL、Elasticsearch、Neo4j、Kafka、Zookeeper、Schema Registry 都是 healthy。

### 7. 生成校验和

```bash
md5sum "${SNAP_DIR}/datahub_root.tar" > "${SNAP_DIR}/datahub_root.tar.md5"
ls -lh "${SNAP_DIR}"
```

## 恢复流程

以下恢复流程会替换当前 `/data/datahub`，执行前必须确认当前状态是否还需要留存。

### 1. 停止当前服务

```bash
docker compose -f /data/datahub/docker-compose.yml down
```

### 2. 留存当前目录

```bash
BROKEN_DIR="/data/datahub_broken_$(date +%Y%m%d_%H%M%S)"
mv /data/datahub "${BROKEN_DIR}"
```

### 3. 解压快照

```bash
tar --numeric-owner -xf /data/datahub_snapshots/datahub_lineage_stage1_20260516_142043/datahub_root.tar -C /data
```

解压后应恢复出 `/data/datahub`。

### 4. 启动恢复后的服务

```bash
docker compose -f /data/datahub/docker-compose.yml up -d
```

### 5. 验证恢复结果

```bash
docker compose -f /data/datahub/docker-compose.yml ps
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:9002/
```

如果服务无法启动，优先检查：

- `/data/datahub/mysql` 权限是否仍对应容器内 MySQL 用户。
- `/data/datahub/elasticsearch`、`/data/datahub/kafka`、`/data/datahub/zookeeper` 权限是否仍对应容器内用户。
- `/data/datahub/neo4j` 权限是否仍为 Neo4j 用户。
- `docker-compose.yml` 中镜像版本是否仍可用。

## 注意事项

- 该快照适合同一台服务器、同一 Docker/Compose 环境下快速回滚。
- 跨机器恢复时，需要额外确认镜像、Docker 版本、磁盘路径、UID/GID 和网络端口。
- 快照包包含 compose 配置、脚本和可能的 token/signing key，应限制读取权限。
- 不建议只备份 MySQL。DataHub 的搜索、图谱、异步事件和索引状态还依赖 Elasticsearch、Neo4j、Kafka、Zookeeper 等目录。
- 如果要求不停机，应改用逻辑备份组合，例如 `mysqldump`、Elasticsearch snapshot、Neo4j dump、Kafka topic export，但恢复链路会复杂很多。
