# Docker 服务清单 - neo4j2.dp.data.bj1

> 服务器: neo4j2.dp.data.bj1.wormpex.com (10.253.2.44)
> 生成时间: 2026-05-08

## DataHub 服务

| 容器名 | 镜像 | 状态 | 端口 | CPU | 内存 |
|--------|------|------|------|-----|------|
| datahub-datahub-gms-1 | acryldata/datahub-gms:v1.5.0.4 | healthy | 8080 | 3.02% | 1.44GiB |
| datahub-datahub-frontend-react-1 | acryldata/datahub-frontend-react:v1.5.0.4 | healthy | 9002 | 0.35% | 575.4MiB |
| datahub-datahub-actions-1 | acryldata/datahub-actions:v1.5.0.4-slim | running | - | 0.13% | 122.4MiB |
| datahub-elasticsearch-1 | elasticsearch:7.16.1 | healthy | 9200, 9300 | 3.11% | 886.7MiB / 1GiB |
| datahub-mysql-1 | mysql:8.2 | healthy | 3306, 33060 | 3.80% | 430MiB |
| datahub-neo4j-1 | neo4j:4.4.9-community | healthy | 7474, 7687 | 1.18% | 2.77GiB |
| datahub-broker-1 | confluentinc/cp-kafka:7.9.2 | healthy | 9092 | 4.46% | 545.1MiB |
| datahub-zookeeper-1 | confluentinc/cp-zookeeper:7.9.2 | healthy | 2181, 2888, 3888 | 0.08% | 126.6MiB |
| datahub-schema-registry-1 | confluentinc/cp-schema-registry:7.9.2 | healthy | 8081 | 3.53% | 381.2MiB |

## 其他服务

| 容器名 | 镜像 | 状态 | 端口 | CPU | 内存 |
|--------|------|------|------|-----|------|
| qdrant | qdrant/qdrant:v1.13.4 | running | 6333, 6334 | 1.16% | 1.33GiB |

## 服务架构

```
                    ┌─────────────────────┐
                    │   Frontend (9002)    │
                    │ datahub-frontend     │
                    └──────────┬──────────┘
                               │ HTTP
                               ▼
┌──────────┐    ┌─────────────────────┐    ┌─────────────────────┐
│  Client  │───▶│   GMS API (8080)   │───▶│   Elasticsearch     │
└──────────┘    │   datahub-gms       │    │   (9200)           │
                └─────────┬───────────┘    └─────────────────────┘
                          │
                          │              ┌─────────────────────┐
                          └─────────────▶│      Neo4j          │
                                         │   (7474, 7687)     │
                                         └─────────────────────┘
                          ┌─────────────┐
                          │   MySQL     │
                          │   (3306)    │    ┌─────────────────────┐
                          └─────────────┘───▶│  Schema Registry     │
                                               │   (8081)           │
                                               └──────────┬──────────┘
                                                          │
                          ┌─────────────────────┐         │
                          │       Kafka         │◀────────┘
                          │   Broker (9092)    │
                          └──────────┬──────────┘
                                     │
                          ┌─────────────────────┐
                          │     Zookeeper        │
                          │     (2181)          │
                          └─────────────────────┘
```

## 服务说明

### Core Services

| 服务 | 说明 | 默认账号 |
|------|------|----------|
| GMS (General Metadata Service) | DataHub 核心服务，提供 REST/GraphQL API | - |
| Frontend | DataHub Web UI | datahub / datahub |
| MySQL | 关系数据库，存储元数据 | datahub / datahub |
| Elasticsearch | 搜索引擎和图存储 | - |
| Neo4j | 图数据库 | neo4j / datahub |

### Messaging

| 服务 | 说明 |
|------|------|
| Kafka Broker | 消息队列，处理 MCE/MCL 事件 |
| Zookeeper | Kafka 依赖服务 |
| Schema Registry | Kafka Schema 管理 |

### Actions

| 服务 | 说明 |
|------|------|
| datahub-actions | 执行动作和事件处理 |

## 访问地址

| 服务 | 地址 |
|------|------|
| DataHub UI | http://10.253.2.44:9002 |
| GMS API | http://10.253.2.44:8080 |
| Neo4j Browser | http://10.253.2.44:7474 |
| Elasticsearch | http://10.253.2.44:9200 |
| Schema Registry | http://10.253.2.44:8081 |
| Kafka | 10.253.2.44:9092 |
| Zookeeper | 10.253.2.44:2181 |
| MySQL | 10.253.2.44:3306 |
| Qdrant | http://10.253.2.44:6333 |

## 数据持久化

| 数据卷 | 宿主机路径 |
|--------|-----------|
| Elasticsearch | /data/datahub/elasticsearch |
| MySQL | /data/datahub/mysql |
| Neo4j | /data/datahub/neo4j |
| Kafka | /data/datahub/kafka |
| Zookeeper | /data/datahub/zookeeper/data, /data/datahub/zookeeper/logs |
| Plugins | /data/datahub/plugins |

## 资源占用

- **Total CPU**: ~20%
- **Total Memory**: ~8.5 GiB (不含 Elasticsearch 限制)
- **Disk (Data)**: /data/datahub/*

## 相关文件

- 配置文件: `/data/datahub/docker-compose.yml`
- 部署文档: `xander/docs/datahub-deploy-neo4j2.md`
