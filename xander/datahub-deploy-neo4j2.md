# DataHub 部署指南 - neo4j2.dp.data.bj1

## 环境信息

- **服务器**: neo4j2.dp.data.bj1.wormpex.com (10.253.2.44)
- **版本**: v1.5.0.4 (stable)
- **部署目录**: `/data/datahub`
- **Docker 镜像代理**: hub-proxy.corp.bianlifeng.com

## 前置条件

### 服务器环境
- Docker 26.1.4 已安装
- Docker daemon 已配置镜像代理（/etc/docker/daemon.json）
- 服务器磁盘充足（/data 5.5T 可用）

### 文件传输
- FTP: ftp.ops.blibee.com（用于上传 docker-compose.yml）
- 跳板机: agent-bastion (10.253.40.11:7233)

## 部署步骤

### 1. 创建数据目录

```bash
mkdir -p /data/datahub/plugins
mkdir -p /data/datahub/kafka
mkdir -p /data/datahub/elasticsearch
mkdir -p /data/datahub/mysql
mkdir -p /data/datahub/neo4j
mkdir -p /data/datahub/zookeeper/data
mkdir -p /data/datahub/zookeeper/logs
```

### 2. 修改目录权限

MySQL 使用 uid 999，neo4j 使用 uid 7474：

```bash
chown -R 999:999 /data/datahub/mysql
chown -R 7474:7474 /data/datahub/neo4j
chown -R 1000:1000 /data/datahub/elasticsearch
chown -R 1000:1000 /data/datahub/kafka
chown -R 1000:1000 /data/datahub/zookeeper/data
chown -R 1000:1000 /data/datahub/zookeeper/logs
```

### 3. 准备 docker-compose.yml

主要配置修改点：

1. **数据卷绑定**：所有 volumes 改为绑定到 `/data/datahub/`
2. **版本固定**：使用 `v1.5.0.4` 而非 `head`
3. **Token 签名密钥**：添加 `DATAHUB_TOKEN_SERVICE_SIGNING_KEY` 和 `DATAHUB_TOKEN_SERVICE_SALT`
4. **Neo4j APOC**：设置 `NEO4JLABS_PLUGINS=[]` 避免插件下载超时
5. **GMS Host**：Frontend 配置 `DATAHUB_GMS_HOST=10.253.2.44`（服务器 IP）

关键环境变量：

```yaml
# datahub-gms 和 datahub-upgrade
- DATAHUB_TOKEN_SERVICE_SIGNING_KEY=AccLVCTZXKRmWPGQnvTSqMT6Duz3hAH3c1xsFNkqKz8z
- DATAHUB_TOKEN_SERVICE_SALT=dH5T56hqS5P7GqhUQT2YnKdX4m7E3N8K

# datahub-frontend-react
- DATAHUB_GMS_HOST=10.253.2.44

# neo4j
- NEO4JLABS_PLUGINS=[]
```

### 4. 上传配置文件

本地执行（文件传到 FTP）：

```bash
cd /path/to/docker-compose.yml
put2 docker-compose.yml
```

服务器执行（FTP 下载到本地）：

```bash
cd /data/datahub
get2 docker-compose.yml
```

### 5. 拉取镜像

使用内网镜像代理，DataHub 镜像可能需要重试：

```bash
docker pull acryldata/datahub-gms:v1.5.0.4
docker pull acryldata/datahub-frontend-react:v1.5.0.4
docker pull acryldata/datahub-upgrade:v1.5.0.4
docker pull acryldata/datahub-actions:v1.5.0.4-slim
docker pull acryldata/datahub-kafka-setup:head  # v1.5.0.4 拉取失败，用 head
docker pull confluentinc/cp-zookeeper:7.9.2
docker pull confluentinc/cp-schema-registry:7.9.2
docker pull confluentinc/cp-kafka:7.9.2
docker pull mysql:8.2
docker pull neo4j:4.4.9-community
docker pull elasticsearch:7.16.1
```

### 6. 启动服务

```bash
cd /data/datahub
docker compose up -d
```

### 7. 验证服务

```bash
docker ps  # 所有容器状态应为 healthy
curl http://localhost:8080/health  # GMS 健康检查
curl http://localhost:9002/       # Frontend 首页
```

## 服务地址

| 服务 | 地址 |
|------|------|
| Frontend | http://10.253.2.44:9002 |
| GMS API | http://10.253.2.44:8080 |
| Neo4j | http://10.253.2.44:7474 |
| Elasticsearch | http://10.253.2.44:9200 |
| Kafka | 10.253.2.44:9092 |
| Zookeeper | 10.253.2.44:2181 |
| Schema Registry | 10.253.2.44:8081 |

## 默认账号

- 用户名: `datahub`
- 密码: `datahub`

## 常见问题

### 1. Neo4j unhealthy，卡在 APOC 插件下载
- **解决**：设置 `NEO4JLABS_PLUGINS=[]` 禁用 APOC

### 2. MySQL 建表失败 (error 168)
- **解决**：`chown -R 999:999 /data/datahub/mysql`

### 3. datahub-upgrade 失败 (authentication.tokenService.signingKey must be set)
- **解决**：添加 `DATAHUB_TOKEN_SERVICE_SIGNING_KEY` 和 `DATAHUB_TOKEN_SERVICE_SALT`

### 4. Frontend 502 Bad Gateway
- **原因**：浏览器未配置代理，或 `DATAHUB_GMS_HOST` 配置为 Docker 内部 hostname
- **解决**：Frontend 配置 `DATAHUB_GMS_HOST=10.253.2.44`

### 5. 镜像拉取超时
- **原因**：Docker Hub 直连不稳定
- **解决**：使用内网镜像代理，或重试拉取

## 重新部署

修改配置后：

```bash
cd /data/datahub
get2 docker-compose.yml  # 下载新配置
docker compose up -d      # 重启服务
```

## 停止服务

```bash
cd /data/datahub
docker compose down
```
