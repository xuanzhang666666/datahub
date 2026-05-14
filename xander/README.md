# xander（个人 / BLF DataHub 运维与入仓）

本目录为上游 DataHub 仓库中的**本地扩展**，与官方发行流程无关。

## 目录结构

| 目录 | 内容 |
| --- | --- |
| **`run/`** | 可执行脚本：HMS 入仓见 **`ingest_hive_database_to_datahub.sh`**；另有 `exec_*`（经 bastion）、`verify_*`、`blf_collect_*` 等 |
| **`docs/`** | 说明与规范：`datahub-deploy-neo4j2.md`、`docker-services-neo4j2.md`、`datahub-ingestion-standards.md`（入仓约束） |
| **`python/`** | 需在 neo4j2 / actions 容器旁部署的 Python 工具，如 `sync_partition_stats_to_datahub_trino.py` |
| **`notes/`** | 短说明、可选流程笔记；**运维踩坑**见 `notes/datahub_ops_pitfalls.md`（GMS 地址、delete、Navigate 分叉） |
| **`infra/`** | 基础设施片段，如 `docker-compose.yml`（本机或 neo4j2 侧 compose；已默认 **关闭 GMS telemetry**、**前端 HTTP idleTimeout=300s** 以降低内网噪音与慢 GraphQL 断连） |
| **`scripts/gms-es/`** | 调 GMS（GraphQL/OpenAPI）、查 ES `datasetindex_v2` 的请求体与示例 JSON/shell、URN 片段、`restoreIndices` 辅助说明等 |
| **`scripts/README.md`** | `scripts/` 下子目录说明（当前主要为 `gms-es/`） |
| **`recipes/`** | 当前仅 **`hive_ingest_one_database.yml`**：按环境变量 `HIVE_INGEST_DATABASE` 同步**单个 Hive 库**全表到 DataHub |
| **`hooks/`** | 可选 git hook（仅约束 `xander/` 下文件的提交方式），按需自行链接到 `.git/hooks` |
| **`.gitignore`** | 忽略本地临时文件，如 `tmp_*.ndjson`（勿将 ES bulk 导出等提交进库） |

## Hive 库 → DataHub（唯一入口）

1. 将 `xander/recipes/hive_ingest_one_database.yml` 拷到 neo4j2：`/data/datahub/recipes/`
2. 将 `xander/run/ingest_hive_database_to_datahub.sh` 拷到：`/data/datahub/scripts/` 并 `chmod +x`

```bash
export LINEAGE_PYTHON=/opt/anaconda3/bin/python
# 注意：宿主机 127.0.0.1:8080 可能是前端而非 GMS；务必与 neo4j2 上 docker 端口映射一致。
# 批量删除等 CLI 推荐在容器内执行：见 notes/datahub_ops_pitfalls.md
export DATAHUB_GMS_URL=http://127.0.0.1:8080
sh xander/run/ingest_hive_database_to_datahub.sh data_logistics
```

Jenkins：每个库一个 Job，在 shell 里传入对应库名即可。入仓在 **Jenkins slave（neo4j2）宿主机** 上跑 `python -m datahub ingest`，**不使用** Docker 容器执行 ingest。

仓库根目录 **`run_hive_lineage_on_neo4j2.sh`** 为薄包装，转发至 **`xander/run/ingest_hive_database_to_datahub.sh`**，兼容仍指向根路径的旧 cron/文档（参数为 **Hive 库名**）。
