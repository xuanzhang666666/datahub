# xander（BLF DataHub / Hive 运维扩展）

与官方 DataHub 发行无关的个人或团队扩展；**当前仅维护两条 Jenkins 任务**所用文件。

## Jenkins 任务与仓库路径

### 1）批量调度血缘 → DataHub（LLM + 校验）

- **入口脚本**：[`python/scripts/run_batch_lineage_sync.sh`](python/scripts/run_batch_lineage_sync.sh)  
  服务器常见路径：`/data/datahub/scripts/run_batch_lineage_sync.sh`
- **Python 包**：[`python/job_info_sync_datahub/`](python/job_info_sync_datahub/)（与脚本同级的 `job_info_sync_datahub/` 目录，或 `JOB_INFO_SYNC_DIR`）
- **环境变量示例**：`PREFIX`、`CONCURRENCY`、`LINEAGE_PYTHON` 等（见脚本内注释）

### 2）Hive 表清单 xlsx → 切分 → 串行 ingest

- **入口脚本**：[`run/ingest_hive_table_list_serial_from_xlsx.sh`](run/ingest_hive_table_list_serial_from_xlsx.sh)  
  服务器常见路径：`/data/datahub/scripts/ingest_hive_table_list_serial_from_xlsx.sh`
- **依赖（同目录 `run/`）**：
  - `split_hive_tables_xlsx.sh` / `split_hive_tables_xlsx.py`
  - `export_hive_table_list_from_xlsx.sh` / `export_hive_table_list_from_xlsx.py`
  - `ingest_hive_table_list_to_datahub.sh`
  - `render_hive_ingest_recipe.py`（生成临时 recipe，**不依赖**手写 yml 模板即可跑）
- **参考 recipe**（可选，与生成结果对照）：[`recipes/hive_ingest_one_database.yml`](recipes/hive_ingest_one_database.yml)

## 目录一览（精简后）

| 路径 | 用途 |
| --- | --- |
| `python/scripts/` | `run_batch_lineage_sync.sh` |
| `python/job_info_sync_datahub/` | 批量血缘 Python 包 |
| `run/` | 上表 xlsx 串行 ingest 链路的 shell/py |
| `recipes/` | 参考用 Hive ingest YAML |
| `archive/` | 已下线脚本、旧文档、gms-es 实验、迁出的单测等（见 [`archive/README.md`](archive/README.md)） |

## 兼容根脚本

根目录 [`run_hive_lineage_on_neo4j2.sh`](../run_hive_lineage_on_neo4j2.sh) 仍转发到**归档内**的旧「按库名 HMS 入仓」脚本，供历史 cron 使用；新任务请用 **任务 2** 的 xlsx 串行链路。

## neo4j2 部署约定

脚本/py 拷到 `/data/datahub/scripts/`，参考 recipe 可拷到 `/data/datahub/recipes/`。内网用 **put2 → get2** 同步，详见 `.cursor/rules/xander-neo4j2-deploy.mdc`。
