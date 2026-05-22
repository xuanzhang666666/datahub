# xander（BLF DataHub / Hive 运维扩展）

与官方 DataHub 发行无关的个人或团队扩展；**当前维护若干 Jenkins 任务**所用文件（见下文）。

## Jenkins 任务与仓库路径

### 1）批量调度血缘 → DataHub（LLM + 校验）

- **入口脚本**：`[python/scripts/run_batch_lineage_sync.sh](python/scripts/run_batch_lineage_sync.sh)`  
服务器常见路径：`/data/datahub/scripts/run_batch_lineage_sync.sh`
- **Python 包**：`[python/job_info_sync_datahub/](python/job_info_sync_datahub/)`（与脚本同级的 `job_info_sync_datahub/` 目录，或 `JOB_INFO_SYNC_DIR`）；逻辑与测试说明见包内 **`ARCHITECTURE.md`**、**`TESTING.md`**。
- **修改包后、上传 neo4j2 前**：在 `xander/python` 下执行 **`sh scripts/run_job_info_sync_datahub_tests.sh`**（需 pytest；默认只跑不依赖 trino 的门禁用例，通过后再 `put2`）。
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
- **ETL 脚本双源**（neo4j2）：`BLF_ETL_LOCAL_ROOT=/localfolder`（默认）；目录名为 `shell_command` 中的 `gitlab_name`（如 `analysis-jobs`）。GitLab 与 local 均命中时内容相同用 GitLab，不同用 local。`BLF_ETL_LOCAL_DISABLE=1` 可仅走 GitLab。**未在 GitLab 映射表中的 `gitlab_name`**（如 `data_dev`、`data_shop`）：只要 `/localfolder/<gitlab_name>/` 目录存在，即仅从 localfolder 拉 ETL，不再要求配置 `GITLAB_NAME_TO_PROJECT_PATH`。
- **Structured Properties**：`blf.data.schedule.execute_shell`（Execute Shell，富文本）须在 GMS 预建；仅当作业解析到表级血缘且 `write_upstream_lineage` 为真时写入 DMP 完整 `shell_command`。
- **表名别名**：LLM 若解析到 `not_verified_<真实表名>`，写入前会去掉 `not_verified_` 前缀再校验并写血缘（与 HMS ingest 排除 `not_verified_.`* 一致）。
- **shell 解析**：`w-run-task.sh` 后的单字母分区、`| …` 管道、**`$VAR` / `${VAR}`**（含与路径**无空白粘连**、或**非常规空白**分隔）均不会进入 `.job`/`.py` 基名（见 `runtime_parser._strip_shell_var_suffix` 与 `extract_job_path_and_type`）。
- **fqtn 表名**：仅字母/数字/下划线，且不能以数字开头（LLM 误解析的 `${date}`、`001_...` 等会被过滤）。
- **本地调试**：`[python/scripts/debug_job_lineage.py](python/scripts/debug_job_lineage.py)`（或 `job_info_sync_datahub.debug_lineage`）分阶段输出 DMP / ETL / LLM / fqtn / Hive / URN，支持 `--format json`、多作业、`--llm-raw` 复用。
- **调度 shell 批量解析作业文件名**（对照血缘逻辑）：`[python/scripts/jenkins_shell_to_jobfile_report.py](python/scripts/jenkins_shell_to_jobfile_report.py)` — 读 xlsx 第 1 列 job 名、第 2 列 `shell_command`，输出 `job_file_name` / `first_gitlab_candidate` / `status`（`ok` / `no_runner` / `parse_job_path` 等）到结果 xlsx，便于统计「本应能解析却找不到 .job/.py」的作业。

### 1b）手动追加一条 Hive 表级上游血缘（Jenkins 参数）

- **入口脚本**：`[python/scripts/run_add_manual_upstream_lineage.sh](python/scripts/run_add_manual_upstream_lineage.sh)`（服务器常见路径：`/data/datahub/scripts/run_add_manual_upstream_lineage.sh`，与批量任务同目录）
- **Jenkins**：与 `run_batch_lineage_sync.sh` 一样用 **Execute shell** 里 `export` + `sh`；**不要**再写 `cd xander/python` / `PYTHONPATH=. python3 -m ...`。
- **Jenkins 参数（建议「Choice」或「String」注入为环境变量）**：`TABLE_NAME`（下游）、`UPSTREAM_NAME`（上游）。**两者都必须为 `库.表`**（至少含一个 `.`），否则脚本直接报错退出；不支持仅表名、也不支持用环境变量补默认库。`DATAHUB_GMS_URL` / `DATAHUB_GMS_TOKEN` 可与批量任务相同：写在脚本同目录的 `lineage.env`（或 `LINEAGE_ENV_FILE` 指向的文件），本脚本会自动 `source`。
- **表名格式**：`db.table`，多段库名可用 `catalog.db.table`。非法示例：`dw_order_v1`、`.tbl`、`db.`。
- **行为**：默认**合并**已有 `upstreamLineage`（按 dataset URN 去重），并尽量保留 `fineGrainedLineages`。若该上游已存在则跳过。`REPLACE=1` 时等价 `--replace`（**仅保留本条上游**，清空其余表级与字段级血缘，慎用）。
- **依赖**：`acryl-datahub`（含 `DataHubGraph`），与批量血缘相同；推荐 `export LINEAGE_PYTHON=/opt/anaconda3/bin/python`。

**Jenkins Execute shell 示例**（对齐 `PREFIX` / `LINEAGE_PYTHON` 写法）：

```bash
export TABLE_NAME=dw.dw_order_v1
export UPSTREAM_NAME=dw.dw_order_v1_archive
# export DRY_RUN=1              # 试跑不写 GMS

export LINEAGE_PYTHON=/opt/anaconda3/bin/python

sh /data/datahub/scripts/run_add_manual_upstream_lineage.sh
```

若参数来自 Jenkins「参数化构建」，可写成：`export TABLE_NAME="${TABLE_NAME}"` 等。

**本地 / 排障**（仍需 `PYTHONPATH` 时）：`cd xander/python && PYTHONPATH=. python3 -m job_info_sync_datahub.manual_upstream_lineage --table-name 库.表 --upstream-name 库.表`

### 1c）Jenkins：按 JOBS 参数同步指定 Hive 表到 DataHub

- **入口脚本**：`[python/scripts/run_jenkins_hive_table_ingest_from_jobs.sh](python/scripts/run_jenkins_hive_table_ingest_from_jobs.sh)`  
  服务器路径：`/data/datahub/scripts/run_jenkins_hive_table_ingest_from_jobs.sh`
- **Jenkins 参数**：**Multi-line String `JOBS`**，每行一张表 `库.表`（如 `data_sec_dw.dim_store_info`）；也支持仅表名（需 `HIVE_INGEST_IMPLICIT_DATABASE`）。
- **行为**：名单中每张表若已在 DataHub → **hard delete** → 再 **批量 HMS ingest**；**不做** fqtn 层级前缀等表名校验。
- **Execute shell 示例**：

```bash
export JOBS="${JOBS}"
export LINEAGE_PYTHON=/opt/anaconda3/bin/python
export DATAHUB_GMS_URL=http://localhost:8080
# export DRY_RUN=1
sh /data/datahub/scripts/run_jenkins_hive_table_ingest_from_jobs.sh
```

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
| `python/scripts/`               | `run_batch_lineage_sync.sh`、`run_batch_lineage_sync_job_list.sh`、`run_add_manual_upstream_lineage.sh`、`run_jenkins_hive_table_ingest_from_jobs.sh`、`run_job_info_sync_datahub_tests.sh` |
| `python/job_info_sync_datahub/` | 批量血缘 Python 包                                                                     |
| `run/`                          | 上表 xlsx 串行 ingest 链路的 shell/py                                                    |
| `in/`                           | Hive 表清单 **xlsx 输入**（与线上 `/data/datahub/in/` 对应，见 `[in/README.md](in/README.md)`） |
| `recipes/`                      | 参考用 Hive ingest YAML                                                              |
| `archive/`                      | 已下线脚本、旧文档、gms-es 实验、迁出的单测等（见 `[archive/README.md](archive/README.md)`）            |
| `docs/`                         | 方案调研（如 [自环表级血缘计划](docs/datahub-self-loop-lineage-plan.md)，**已搁置**）                    |


## 兼容根脚本

根目录 `[run_hive_lineage_on_neo4j2.sh](../run_hive_lineage_on_neo4j2.sh)` 仍转发到**归档内**的旧「按库名 HMS 入仓」脚本，供历史 cron 使用；新任务请用 **任务 2** 的 xlsx 串行链路。

## neo4j2 部署约定

脚本/py 拷到 `/data/datahub/scripts/`，参考 recipe 可拷到 `/data/datahub/recipes/`。内网用 **put2 → get2** 同步，详见 `.cursor/rules/xander-neo4j2-deploy.mdc`。**打包上传 `job_info_sync_datahub` 前**，建议在服务器或本机（含 pytest 的 Python）执行：`cd /data/datahub/scripts` 所在仓库镜像目录下的 `xander/python`，运行 `sh scripts/run_job_info_sync_datahub_tests.sh`（或设 `LINEAGE_PYTHON`），通过后再传 tar。