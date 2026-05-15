# xander（BLF DataHub / Hive 运维扩展）

与官方 DataHub 发行无关的个人或团队扩展；**当前仅维护两条 Jenkins 任务**所用文件。

## Jenkins 任务与仓库路径

### 1）批量调度血缘 → DataHub（LLM + 校验）

- **入口脚本**：`[python/scripts/run_batch_lineage_sync.sh](python/scripts/run_batch_lineage_sync.sh)`  
服务器常见路径：`/data/datahub/scripts/run_batch_lineage_sync.sh`
- **Python 包**：`[python/job_info_sync_datahub/](python/job_info_sync_datahub/)`（与脚本同级的 `job_info_sync_datahub/` 目录，或 `JOB_INFO_SYNC_DIR`）
- **环境变量示例**：`PREFIX`、`CONCURRENCY`、`LINEAGE_PYTHON` 等（见脚本内注释）
- **按作业名单重跑**（不用 `PREFIX`，独立报告，默认强制重跑）：
  - **入口脚本**：`[python/scripts/run_batch_lineage_sync_job_list.sh](python/scripts/run_batch_lineage_sync_job_list.sh)`
  - **Jenkins**：增加 **Multi-line String** 参数 `JOBS`，每行一个 `job_display_name`；Build 命令示例：`sh /data/datahub/scripts/run_batch_lineage_sync_job_list.sh`
  - **报告**（仅清空/写入以下文件，**不会**动全量 `batch_report.jsonl`）：
    - `lineage_reports/batch_report_job_list.jsonl`
    - `lineage_reports/lineage_audit_job_list.jsonl`
    - `lineage_reports/lineage_report_job_list.xlsx`
  - **可选**：`JOB_FILE` / 脚本第一个参数为名单文件；`JOB_LIST_FILE` 覆盖 JOBS 落盘路径；`JOB_LIST_REPORT` 覆盖批次 jsonl；`JOB_LIST_CLEAR=0` 断点续跑；名单支持 `#` 注释与空行
  - **本地示例**：
    ```bash
    export JOBS=$'dim_takeaway_region_manager_info\n# comment\npdw_foo_bar_di'
    export CONCURRENCY=10
    sh python/scripts/run_batch_lineage_sync_job_list.sh
    ```
- **ETL 脚本双源**（neo4j2）：`BLF_ETL_LOCAL_ROOT=/localfolder`（默认）；目录名为 `shell_command` 中的 `gitlab_name`（如 `analysis-jobs`）。GitLab 与 local 均命中时内容相同用 GitLab，不同用 local。`BLF_ETL_LOCAL_DISABLE=1` 可仅走 GitLab。
- **Structured Properties**：`blf.data.schedule.execute_shell`（Execute Shell，富文本）须在 GMS 预建；仅当作业解析到表级血缘且 `write_upstream_lineage` 为真时写入 DMP 完整 `shell_command`。
- **表名别名**：LLM 若解析到 `not_verified_<真实表名>`，写入前会去掉 `not_verified_` 前缀再校验并写血缘（与 HMS ingest 排除 `not_verified_.`* 一致）。
- **shell 解析**：`w-run-task.sh` 后的单字母调度参数（如 `D`）与 `| ...` 管道不会进入 `.job` 文件名（见 `runtime_parser.extract_job_path_and_type`）。
- **fqtn 表名**：仅字母/数字/下划线，且不能以数字开头（LLM 误解析的 `${date}`、`001_...` 等会被过滤）。
- **本地调试**：`[python/scripts/debug_job_lineage.py](python/scripts/debug_job_lineage.py)`（或 `job_info_sync_datahub.debug_lineage`）分阶段输出 DMP / ETL / LLM / fqtn / Hive / URN，支持 `--format json`、多作业、`--llm-raw` 复用。

### 2）Hive 表清单 xlsx → 切分 → 串行 ingest

- **入口脚本**：`[run/ingest_hive_table_list_serial_from_xlsx.sh](run/ingest_hive_table_list_serial_from_xlsx.sh)`  
服务器常见路径：`/data/datahub/scripts/ingest_hive_table_list_serial_from_xlsx.sh`
- **依赖（同目录 `run/`）**：
  - `split_hive_tables_xlsx.sh` / `split_hive_tables_xlsx.py`
  - `export_hive_table_list_from_xlsx.sh` / `export_hive_table_list_from_xlsx.py`
  - `ingest_hive_table_list_to_datahub.sh`
  - `render_hive_ingest_recipe.py`（生成临时 recipe，**不依赖**手写 yml 模板即可跑）
- **参考 recipe**（可选，与生成结果对照）：`[recipes/hive_ingest_one_database.yml](recipes/hive_ingest_one_database.yml)`
- **表清单 xlsx（本地副本）**：`[in/](in/)`（如 `[in/20260512-hive-tables.xlsx](in/20260512-hive-tables.xlsx)`，对应线上 `HIVE_XLSX_IN`）

## 目录一览（精简后）


| 路径                              | 用途                                                                                |
| ------------------------------- | --------------------------------------------------------------------------------- |
| `python/scripts/`               | `run_batch_lineage_sync.sh`、`run_batch_lineage_sync_job_list.sh`                 |
| `python/job_info_sync_datahub/` | 批量血缘 Python 包                                                                     |
| `run/`                          | 上表 xlsx 串行 ingest 链路的 shell/py                                                    |
| `in/`                           | Hive 表清单 **xlsx 输入**（与线上 `/data/datahub/in/` 对应，见 `[in/README.md](in/README.md)`） |
| `recipes/`                      | 参考用 Hive ingest YAML                                                              |
| `archive/`                      | 已下线脚本、旧文档、gms-es 实验、迁出的单测等（见 `[archive/README.md](archive/README.md)`）            |


## 兼容根脚本

根目录 `[run_hive_lineage_on_neo4j2.sh](../run_hive_lineage_on_neo4j2.sh)` 仍转发到**归档内**的旧「按库名 HMS 入仓」脚本，供历史 cron 使用；新任务请用 **任务 2** 的 xlsx 串行链路。

## neo4j2 部署约定

脚本/py 拷到 `/data/datahub/scripts/`，参考 recipe 可拷到 `/data/datahub/recipes/`。内网用 **put2 → get2** 同步，详见 `.cursor/rules/xander-neo4j2-deploy.mdc`。