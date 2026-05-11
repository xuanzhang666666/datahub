# xander（个人 / BLF DataHub 运维与入仓）

本目录为上游 DataHub 仓库中的**本地扩展**，与官方发行流程无关。结构说明如下。

| 路径 | 用途 |
| --- | --- |
| `docs/` | 说明与规范（如 neo4j2 部署、`datahub-ingestion-standards.md` 入仓约束） |
| `run/` | 可执行脚本：Hive metastore 入仓、分区统计、bastion 触发、校验等 |
| `recipes/` | `datahub ingest -c` 用的 YAML recipe |
| `scripts/gms-es/` | GMS GraphQL 请求体、Elasticsearch 查询 JSON、`restoreIndices` 辅助片段 |
| `python/` | 需在 neo4j2 / actions 容器旁部署的 Python 工具 |
| `notes/` | 短说明、可选流程笔记 |
| `infra/` | 例如 `docker-compose.yml`（本机 / neo4j2 侧 compose 片段） |
| `hooks/` | 可选 git hook（仅约束 `xander/` 下文件的提交方式） |

从仓库根目录调用示例：`sh xander/run/run_hive_metastore_dw_order_v1_lineage_ingest.example.sh`（需先设置 `HMS_THRIFT_HOST`、`DATAHUB_GMS_URL` 等）。
