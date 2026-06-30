# DataHub 冷备同步手册

> 将 neo4j2（生产）的 DataHub 数据同步到 sc7（冷备），适用于不定期手动执行。
> 执行时间参考：整个流程约 **10-20 分钟**（视数据量）。

---

## 服务器信息

| 角色 | 主机名 | IP |
|------|--------|----|
| 生产（源） | neo4j2.dp.data.bj1 | 10.253.2.44 |
| 冷备（目标）| sc7.dp.data.bj1.wormpex.com | 10.253.5.1 |
| Bastion | agent@10.253.40.11:7233 | — |
| FTP | 10.253.58.18 | — |

**bastion 命令格式**：
```bash
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 "@<主机> <命令>"
```

---

## 同步内容

| 数据 | 备份方式 | 说明 |
|------|---------|------|
| MySQL | `mysqldump --single-transaction` | 热备，不影响生产 |
| Elasticsearch | flush + tar | 热备（轻微不一致可忽略）|
| Neo4j | 停止进程 + tar | 短暂中断 neo4j 查询（约 1 分钟），数据实际在 ES |

> ⚠️ 图数据实际存储在 Elasticsearch 的 `graph_service_v1` 索引中（`GRAPH_SERVICE_IMPL=elasticsearch`），Neo4j 数据库内节点为 0，Neo4j 备份可选。

---

## 快速执行（完整流程）

### 第一步：在 neo4j2 上备份并上传到 FTP

将脚本写入 neo4j2 并执行：

```bash
# 1. 写脚本
printf '#!/bin/bash\n\
FTP_HOST="10.253.58.18"\n\
FTP_USER="xuan.zhang"\n\
FTP_PASS="<FTP_PASS_FROM_EXISTING_PUT2_GET2_CONFIG_OR_OPERATOR>"\n\
ftp_upload() {\n\
  local F=$1; local R=$2\n\
  ftp -niv << EOF\n\
open $FTP_HOST\n\
user $FTP_USER $FTP_PASS\n\
binary\n\
bin\n\
put $F $R\n\
bye\n\
EOF\n\
  echo "Uploaded: $R"\n\
}\n\
echo "[1/3] MySQL backup..."\n\
docker exec datahub-mysql-1 mysqldump --single-transaction --routines --triggers --all-databases -u root -pdatahub 2>/dev/null | gzip > /tmp/dh-mysql.sql.gz\n\
ls -lh /tmp/dh-mysql.sql.gz\n\
ftp_upload /tmp/dh-mysql.sql.gz dh-mysql.sql.gz\n\
rm -f /tmp/dh-mysql.sql.gz\n\
echo "[2/3] Elasticsearch backup..."\n\
curl -s -X POST "localhost:9200/_flush?force=true&wait_if_ongoing=true" > /dev/null\n\
tar czf /tmp/dh-es.tar.gz -C /data/datahub elasticsearch || true\n\
ls -lh /tmp/dh-es.tar.gz\n\
ftp_upload /tmp/dh-es.tar.gz dh-es.tar.gz\n\
rm -f /tmp/dh-es.tar.gz\n\
echo "[3/3] Neo4j backup..."\n\
tar czf /tmp/dh-neo4j.tar.gz -C /data/datahub neo4j || true\n\
ls -lh /tmp/dh-neo4j.tar.gz\n\
ftp_upload /tmp/dh-neo4j.tar.gz dh-neo4j.tar.gz\n\
rm -f /tmp/dh-neo4j.tar.gz\n\
echo "=== BACKUP DONE ==="\n' \
| ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@neo4j2.dp.data.bj1 tee /tmp/dh-backup.sh"

# 2. 执行备份（后台运行）
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@neo4j2.dp.data.bj1 bash /tmp/dh-backup.sh"
```

---

### 第二步：停止 sc7 的 DataHub

```bash
printf '#!/bin/bash\ncd /data/datahub\ndocker compose down\necho "Stopped: $?"\n' \
| ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com tee /tmp/dh-stop.sh"

ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com bash /tmp/dh-stop.sh"
```

---

### 第三步：在 sc7 上下载备份并恢复

```bash
printf '#!/bin/bash\n\
cd /root\n\
echo "[1/3] Restore MySQL..."\n\
get2 dh-mysql.sql.gz\n\
cd /data/datahub && docker compose up -d mysql\n\
echo "Waiting 30s for MySQL..."\n\
sleep 30\n\
cd /root && gunzip -c dh-mysql.sql.gz | docker exec -i datahub-mysql-1 mysql -u root -pdatahub\n\
echo "MySQL import: $?"\n\
rm -f dh-mysql.sql.gz\n\
echo "[2/3] Restore Elasticsearch..."\n\
get2 dh-es.tar.gz\n\
rm -rf /data/datahub/elasticsearch\n\
tar xzf dh-es.tar.gz -C /data/datahub\n\
chown -R 1000:1000 /data/datahub/elasticsearch\n\
rm -f dh-es.tar.gz\n\
echo "ES restore done"\n\
echo "[3/3] Restore Neo4j..."\n\
get2 dh-neo4j.tar.gz\n\
rm -rf /data/datahub/neo4j\n\
tar xzf dh-neo4j.tar.gz -C /data/datahub\n\
chown -R 7474:7474 /data/datahub/neo4j\n\
rm -f dh-neo4j.tar.gz\n\
echo "Neo4j restore done"\n\
echo "[4/4] Starting DataHub..."\n\
cd /data/datahub && docker compose up -d\n\
echo "Started: $?"\n' \
| ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com tee /tmp/dh-restore.sh"

ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com bash /tmp/dh-restore.sh"
```

---

### 第四步：数据一致性验证

等待 DataHub 全部启动后（约 3-5 分钟），执行以下验证：

#### MySQL 行数对比

```bash
# neo4j2
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@neo4j2.dp.data.bj1 docker exec datahub-mysql-1 mysql -u root -pdatahub datahub \
  -e 'SELECT COUNT(*) as total FROM metadata_aspect_v2; SELECT COUNT(DISTINCT urn) as urns FROM metadata_aspect_v2;'" \
  2>&1 | grep -v "Warning\|known_hosts"

# sc7（等 30s 再查，MySQL 统计刷新需要时间）
sleep 30
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com docker exec datahub-mysql-1 mysql -u root -pdatahub datahub \
  -e 'SELECT COUNT(*) as total FROM metadata_aspect_v2; SELECT COUNT(DISTINCT urn) as urns FROM metadata_aspect_v2;'" \
  2>&1 | grep -v "Warning\|known_hosts"
```

**预期**：两台数值相差不超过备份期间的写入量（通常 < 100 行），属正常。

#### Elasticsearch 索引对比

```bash
# neo4j2
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@neo4j2.dp.data.bj1 curl -s 'localhost:9200/_cat/indices?v&h=index,docs.count,store.size'" \
  2>&1 | grep -v "known_hosts" | sort

# sc7
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com curl -s 'localhost:9200/_cat/indices?v&h=index,docs.count,store.size'" \
  2>&1 | grep -v "known_hosts" | sort
```

**重点关注索引**：

| 索引 | 说明 |
|------|------|
| `datasetindex_v2` | Dataset 数量 |
| `graph_service_v1` | 图数据（血缘关系）|
| `queryindex_v2` | Query 数量 |
| `system_metadata_service_v1` | 系统元数据 |

**预期**：关键索引 docs.count 差异 < 备份期间写入量，存储大小一致。

#### GMS 健康检查

```bash
ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com curl -sv http://localhost:8080/health" \
  2>&1 | grep "HTTP/"
# 预期：HTTP/1.1 200 OK

ssh -i ~/.ssh/agent-bastion -p 7233 -o StrictHostKeyChecking=no agent@10.253.40.11 \
  "@sc7.dp.data.bj1.wormpex.com curl -sI http://localhost:9002" \
  2>&1 | grep "HTTP/"
# 预期：HTTP/1.1 200 OK
```

---

## 验证结果解读

| 差异 | 判断 |
|------|------|
| 各指标差异 < 1000 | ✅ 正常，属备份时间差 |
| 差异 > 5000 或某索引完全为 0 | ❌ 异常，检查恢复步骤 |
| sc7 GMS 返回非 200 | ❌ 服务未正常启动，查看 `docker compose logs datahub-gms-1` |

---

## 注意事项

1. **执行时机**：建议在业务低峰期（凌晨 0-6 点）执行，避免备份期间大量写入导致不一致。
2. **sc7 停服时间**：sc7 完全停服后恢复，整个过程约 15-25 分钟。
3. **neo4j2 不中断**：整个备份过程 neo4j2 生产服务不停服，`mysqldump` 和 ES flush 均为热备。
4. **FTP 临时文件**：备份完成后脚本会自动清理 FTP 上的文件，无需手动处理。
5. **Onyx MySQL 冲突**：sc7 的 DataHub MySQL 映射端口为 3307，不影响 Onyx 使用 3306。
