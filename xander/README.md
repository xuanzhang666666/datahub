# xander（个人 / BLF DataHub 运维与入仓）

本目录为上游 DataHub 仓库中的**本地扩展**，与官方发行流程无关。

## 目录结构

| 目录 | 内容 |
| --- | --- |
| **`run/`** | 可执行脚本：`run_hive_*`（neo4j2 上 HMS 入仓等）、`exec_*`（经 bastion 触发）、`verify_*`、`blf_collect_*` 等 |
| **`docs/`** | 说明与规范：`datahub-deploy-neo4j2.md`、`docker-services-neo4j2.md`、`datahub-ingestion-standards.md`（入仓约束） |
| **`python/`** | 需在 neo4j2 / actions 容器旁部署的 Python 工具，如 `sync_partition_stats_to_datahub_trino.py` |
| **`notes/`** | 短说明、可选流程笔记，如 `openlineage_optional_note.txt` |
| **`infra/`** | 基础设施片段，如 `docker-compose.yml`（本机或 neo4j2 侧 compose） |
| **`scripts/gms-es/`** | 调 GMS（GraphQL/OpenAPI）、查 ES `datasetindex_v2` 的请求体与示例 JSON/shell、URN 片段、`restoreIndices` 辅助说明等 |
| **`scripts/README.md`** | `scripts/` 下子目录说明（当前主要为 `gms-es/`） |
| **`recipes/`** | `datahub ingest -c` 使用的 YAML recipe（如各 `hive_metastore_*.yml`） |
| **`hooks/`** | 可选 git hook（仅约束 `xander/` 下文件的提交方式），按需自行链接到 `.git/hooks` |
| **`.gitignore`** | 忽略本地临时文件，如 `tmp_*.ndjson`（勿将 ES bulk 导出等提交进库） |

仓库根目录可能保留 **`hive_metastore_dw_order_v1_lineage.yml`** 等与 `put2`/扁平文件名部署对齐的副本，与 `recipes/` 中同名 recipe 可同时维护；以实际 diff 为准。

## 使用示例

从仓库根目录执行（需先设置 `HMS_THRIFT_HOST`、`DATAHUB_GMS_URL` 等）：

```bash
sh xander/run/run_hive_metastore_dw_order_v1_lineage_ingest.example.sh
```

根目录 **`run_hive_lineage_on_neo4j2.sh`** 为薄包装，转发至 **`xander/run/run_hive_lineage_on_neo4j2.sh`**，兼容仍指向仓库根路径的旧文档或 cron。
