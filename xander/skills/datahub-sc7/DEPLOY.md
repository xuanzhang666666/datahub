# DataHub 新服务器部署指南

> 本文档记录在 sc7（10.253.5.1）上从零部署 DataHub v1.5.0.4 的完整过程，以便在新机器上复现。

---

## 前提条件

| 项目 | 要求 |
|------|------|
| OS | CentOS 7 / Rocky 8（内核 3.10+） |
| Docker | 26.x（与源服务器版本一致） |
| 可用内存 | ≥ 100GB（GMS 32G + Neo4j 48G + ES 12G + 其他） |
| 可用磁盘 | ≥ 100GB（根盘） |
| 网络 | 内网互通，无需公网（镜像通过 FTP 传输） |

---

## 关键信息

| 项目 | 值 |
|------|-----|
| 源服务器（生产）| neo4j2.dp.data.bj1（10.253.2.44）|
| 目标服务器 | sc7.dp.data.bj1.wormpex.com（10.253.5.1）|
| DataHub 版本 | v1.5.0.4 |
| 自定义 Frontend 镜像 | `datahub-frontend-react:lineage-sidebar-v1504-j17-v3-202605191037` |
| 数据目录 | `/data/datahub/` |
| MySQL 宿主机端口 | **3307**（sc7 的 3306 已被 Onyx 占用）|
| 前端访问端口 | 9002 |
| GMS 端口 | 8080 |

---

## Step 1：创建数据目录

在目标服务器上执行：

```bash
mkdir -p /data/datahub/{mysql,elasticsearch,neo4j,kafka,zookeeper/data,zookeeper/logs,plugins,recipes,scripts,in,out,export,reports}
# 修复非 root 容器的目录权限
chmod 777 /data/datahub/zookeeper/data /data/datahub/zookeeper/logs
chmod 777 /data/datahub/elasticsearch /data/datahub/kafka
```

---

## Step 2：准备 docker-compose.yml

将 `/data/datahub/docker-compose.yml` 上传到目标服务器（文件在本仓库 `datahub-sc7/docker-compose.yml`）。

**与 neo4j2 的差异（必须修改）**：

| 配置项 | neo4j2 | sc7 |
|--------|--------|-----|
| `DATAHUB_GMS_HOST`（frontend 服务）| `10.253.2.44` | 目标服务器 IP |
| MySQL 宿主机端口 | `3306:3306` | `3307:3306`（如 3306 已被占用）|

上传命令（通过 agent-bastion）：
```bash
cat /path/to/docker-compose.yml | ssh -i ~/.ssh/agent-bastion -p 7233 \
  agent@10.253.40.11 "@<目标主机> tee /data/datahub/docker-compose.yml"
```

---

## Step 3：转移 Docker 镜像

sc7 无公网，所有镜像需从 neo4j2 通过 FTP 转移。

### 3a. 在 neo4j2 上打包并上传（使用脚本，避免 bastion 管道限制）

将以下脚本写入 neo4j2 的 `/tmp/save-images.sh` 并执行：

```bash
#!/bin/bash
FTP_HOST="10.253.58.18"
FTP_USER="xuan.zhang"
FTP_PASS="<密码>"  # 见 put2 脚本

ftp_upload() {
  local LOCAL=$1; local REMOTE=$2
  ftp -niv << EOF
open $FTP_HOST
user $FTP_USER $FTP_PASS
binary
bin
put $LOCAL $REMOTE
bye
EOF
}

images=(
  "datahub-frontend-react:lineage-sidebar-v1504-j17-v3-202605191037 dh-frontend.tar.gz"
  "acryldata/datahub-gms:v1.5.0.4 dh-gms.tar.gz"
  "acryldata/datahub-upgrade:v1.5.0.4 dh-upgrade.tar.gz"
  "acryldata/datahub-actions:v1.5.0.4-slim dh-actions.tar.gz"
  "acryldata/datahub-kafka-setup:head dh-kafka-setup.tar.gz"
  "confluentinc/cp-kafka:7.9.2 cp-kafka.tar.gz"
  "confluentinc/cp-schema-registry:7.9.2 cp-schema-registry.tar.gz"
  "confluentinc/cp-zookeeper:7.9.2 cp-zookeeper.tar.gz"
  "elasticsearch:7.16.1 elasticsearch.tar.gz"
  "neo4j:4.4.9-community neo4j.tar.gz"
  "mysql:8.2 mysql.tar.gz"
)

for entry in "${images[@]}"; do
  img=$(echo $entry | awk '{print $1}')
  file=$(echo $entry | awk '{print $2}')
  echo "Saving $img -> $file ..."
  docker save $img | gzip > /tmp/$file
  ftp_upload /tmp/$file $file
  rm -f /tmp/$file
  echo "Done: $file"
done
echo "ALL DONE"
```

### 3b. 在 sc7 上下载并加载

将以下脚本写入 sc7 的 `/tmp/load-images.sh` 并执行：

```bash
#!/bin/bash
cd /root
files=(
  dh-frontend.tar.gz
  dh-gms.tar.gz
  dh-upgrade.tar.gz
  dh-actions.tar.gz
  dh-kafka-setup.tar.gz
  cp-kafka.tar.gz
  cp-schema-registry.tar.gz
  cp-zookeeper.tar.gz
  elasticsearch.tar.gz
  neo4j.tar.gz
  mysql.tar.gz
)

for f in "${files[@]}"; do
  echo "Downloading $f ..."
  get2 $f
  echo "Loading $f ..."
  docker load < $f
  rm -f $f
  echo "Done: $f"
done
echo "ALL IMAGES LOADED"
docker images | grep -E "datahub|confluent|elastic|neo4j|mysql"
```

---

## Step 4：启动 DataHub

```bash
# 写启动脚本到目标服务器，通过 bash 执行（绕过 bastion 管道限制）
cat << 'EOF' > /tmp/start.sh
#!/bin/bash
cd /data/datahub
docker compose up -d
echo "Exit: $?"
EOF

bash /tmp/start.sh
```

等待约 3-5 分钟，所有服务启动完成。

---

## Step 5：验证

```bash
# GMS 健康检查
curl http://10.253.5.1:8080/health
# 预期：HTTP 200 OK

# 前端检查
curl -I http://10.253.5.1:9002
# 预期：HTTP 200 OK

# 容器状态
docker ps | grep datahub
```

---

## 常见问题

### Zookeeper / Elasticsearch / Kafka 启动失败

通常是目录权限问题（容器使用 UID 1000）：

```bash
chmod 777 /data/datahub/zookeeper/data /data/datahub/zookeeper/logs
chmod 777 /data/datahub/elasticsearch /data/datahub/kafka
```

### sc7 无公网，docker compose up 卡住

sc7 无法访问 Docker Hub，`docker compose up` 会尝试拉取镜像而卡住。
解决方案：确保所有镜像已通过 Step 3 从 neo4j2 转移到位，再执行 `docker compose up -d`。

### MySQL 端口冲突

如目标服务器 3306 已被占用，在 `docker-compose.yml` 的 mysql 服务中改为：
```yaml
ports:
  - "3307:3306"  # 改为其他未占用端口
```
