# PDW 调度作业：拉取 GitLab 脚本并写入 DataHub 结构化属性

本文说明试点脚本 **`xander/python/pilot_pdw_job_etl_script_to_structured_properties.py`** 如何**拿到作业脚本**、如何**解析目标表**、如何**更新 DataHub**（与实现一致，便于运维复用）。

---

## 一、整体流程

1. 用 **Trino** 查 DMP 表 `default.ods_data_platform_dmp_schedule_job_basic_info`（最新 `dt`），按 **`job_display_name`** 取 **`shell_command`**（Base64 解码在 SQL 里完成）。
2. 从 **`shell_command`** 解析 **`gitlab_name`**、**作业路径**、**job 类型**（`w-run-task.sh` / `w-run-task.sh python …`），拼出 GitLab 仓库内 **`.job` / `.py` 路径**，调用 **GitLab API v4** 拉取文件原文。
3. 对脚本内容做 **`VAR="value"`** 形式的大写变量展开（如 `TABLE_NAME="pdw_opc_flag_contact"` → 把 SQL 里的 `$TABLE_NAME` / `${TABLE_NAME}` 换成字面量），再用正则从全文扫 **`INSERT INTO` / `INSERT OVERWRITE TABLE`** 与 **`CREATE TABLE`** 得到 **目标库表**（无库名时库为 **`default`**）。
4. 对每个 **`(db, table)`** 构造 Hive Dataset URN：  
   `urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.<db>.<table>,PROD)`（`platform_instance` / `env` 可用参数覆盖）。
5. 对每个目标 URN 调用 GMS **OpenAPI**：**`PATCH .../structuredProperties`**，写入两条结构化属性（见下文）。

---

## 二、怎么获取脚本（GitLab）

### 2.1 调度元数据（Trino）

- 表：`default.ods_data_platform_dmp_schedule_job_basic_info`
- 分区：`dt = (SELECT max(dt) FROM …)`
- 字段：`try(from_utf8(from_base64(shell_commond))) AS shell_command`
- 连接：环境变量 **`TRINO_HOST`** / **`TRINO_PORT`** / **`TRINO_USER`** / **`TRINO_CATALOG`** / **`TRINO_SCHEMA`**（脚本内已有默认值，可按环境改）。

### 2.2 从 `shell_command` 推导 GitLab 信息（与 `blf-schedule-job-info` / `blf-gitlab-clone-file` 一致）

- 取**最后一条**包含 **`w-run-task.sh`** 的非纯注释行；支持 **`#/home/.../bin/w-run-task.sh`** 这种 shebang 行（去掉行首 `#` 再解析）。
- **`gitlab_name`**：`…/<gitlab_name>/bin/w-run-task.sh` 里 `<gitlab_name>` 为 **`/bin/w-run-task.sh` 前一截路径的最后一段**（例如 `analysis-jobs`）。
- **GitLab 项目路径**：由 `gitlab_name` 查脚本内映射表（如 `analysis-jobs` → `data/analysis-jobs`）；未命中需在脚本 **`GITLAB_NAME_TO_PROJECT_PATH`** 中补行。
- **作业路径**：`w-run-task.sh` 后面、**环境参数**（`prod` / `before` / 纯数字 / `--xxx`）之前的片段；若以 **`python`** 开头则先去掉 `python` 再截断。
- **`job_file_name`**：作业路径里 **`/` → `_`**，再拼 **`.job`** 或 **`.py`**（python 作业）。
- **GitLab 文件路径候选**（依次尝试，失败再换 ref / 再换路径）：
  - `jobs/<第一段路径>/<job_file_name>`（第一段 = 作业路径按 `/` 的第一段）
  - `jobs/<完整作业路径>.job` / `.py`
  - `jobs/<完整作业路径>/<job_file_name>`
- **Ref**：默认 **`master`**，请求失败会再试 **`main`**（文件 API）。
- **私有仓库**：必须设置 **`BLF_GITLAB_PRIVATE_TOKEN`**（或 **`--gitlab-token`**），请求头 **`PRIVATE-TOKEN`**。匿名访问私有项目通常会 **404**，需带 token。
- **仍找不到路径**：若已配置 token，会再调 **`GET /projects/:id/search?scope=blobs&search=<job_file_name>`**，用返回的 **`path`** 再拉文件。

### 2.3 不经过 GitLab（调试）

- **`--etl-file /path/to/file.job`**：直接读本地 UTF-8 文件，跳过 GitLab；仍会走 Trino 取 `shell_command`（用于 schedule URL 与路径解析日志）。
- **`--gitlab-file-path jobs/.../xxx.job`**：跳过自动候选，只用这一条仓库路径。

---

## 三、怎么解析目标表

1. **`expand_job_shell_vars_for_sql`**：匹配行首赋值 **`^[A-Z][A-Z0-9_]*\s*=\s*["']...["']`**，将 **`${VAR}`** 与 **`$VAR`** 在全文替换后再做 SQL 扫描。
2. **`extract_target_tables`**（正则，多匹配、去重）：
   - **`INSERT INTO` / `INSERT OVERWRITE TABLE`** 后的表名；
   - **`CREATE [EXTERNAL] TABLE`** 后的表名。
3. 表名若带 **`.`**，按 **`库.表`** 解析；否则 **`default.表`**。

解析不到任何目标表时，进程以非 0 退出，并打印 **`ABNORMAL_JOB\t<job>\tno INSERT/CREATE TABLE targets parsed`**。

---

## 四、怎么更新到 DataHub

### 4.1 前置条件（Govern）

以下 **结构化属性定义** 需在 DataHub 中已存在（类型等按你们平台约定）：

| 用途 | qualifiedName（脚本中 URN） |
| --- | --- |
| ETL 脚本全文 | `urn:li:structuredProperty:blf.data.warehouse.etl_script` |
| 调度链接 | `urn:li:structuredProperty:blf.data.schedule.schedule_url` |

### 4.2 写入内容与 Schedule URL 规则

- **`etl_script`**：GitLab（或 `--etl-file`）得到的 **完整脚本原文**。
- **`schedule_url`**：固定模板  
  `https://schedule.corp.bianlifeng.com/job/<job_display_name>`  
  其中 **`<job_display_name>`** 来自 DMP 查询结果（与调度系统展示名一致）。

### 4.3 API 与 PATCH 语义

- **URL**：`{DATAHUB_GMS_URL}/openapi/v3/entity/dataset/{urlencode(datasetUrn)}/structuredProperties`
- **Header**：`Content-Type: application/json-patch+json`；若 GMS 需要鉴权则加 **`Authorization: Bearer <token>`**。
- **Body**：JSON Patch 数组 + **`arrayPrimaryKeys.properties = ["propertyUrn"]`**（与同仓库 `sync_partition_stats_to_datahub_trino.py` 一致）。
- **重要**：对「数据集上尚未有过的属性键」应使用 **`"op": "add"`**；若用 **`replace`**，在键尚不存在时 GMS 可能返回 **400**（`Non-existing name/value pair …`）。脚本当前实现为 **`add`**。

每个解析出的 **`(db, table)`** 各 PATCH **一次**（同一脚本全文、同一 `schedule_url` 会写到每个目标数据集上）。

### 4.4 环境变量与命令示例

```bash
# Python 依赖
python3 -m pip install trino

# Trino（按需覆盖）
export TRINO_HOST=10.253.7.167
export TRINO_PORT=8081
export TRINO_USER=xuan.zhang

# GitLab（私有库必填）
export BLF_GITLAB_PRIVATE_TOKEN='glpat-...'

# DataHub GMS
export DATAHUB_GMS_URL='http://datahub-gms:8080'   # 或 https://你的-gms
# export DATAHUB_GMS_TOKEN='...'                  # 无鉴权可不设

# 只演练：不写 GMS
python3 xander/python/pilot_pdw_job_etl_script_to_structured_properties.py --dry-run

# 指定作业名（默认试点为 pdw_opc_flag_contact）
python3 xander/python/pilot_pdw_job_etl_script_to_structured_properties.py --job pdw_opc_flag_contact

# 在 neo4j2 的 datahub-actions 容器内跑（可访问 compose 网络里的 gms + 内网 GitLab/Trino）
# 需先把脚本拷进容器，例如 docker cp / docker exec tee …
```

---

## 五、异常与日志

脚本在失败时向 **stderr** 打印 **`ABNORMAL_JOB\t<job>\t<阶段>:<原因>`**，阶段包括：`trino` / `shell_parse` / `gitlab` / `datahub`。

---

## 六、与「分区统计 PATCH」的关系

同思路的 OpenAPI 示例见：`xander/python/sync_partition_stats_to_datahub_trino.py`（`PATCH structuredProperties` + `arrayPrimaryKeys`）。本试点脚本扩展为：**DMP + GitLab + 目标表解析 + 两条业务结构化属性**。

---

## 七、安全建议

- **不要在仓库、聊天、命令历史中明文长期保存 `BLF_GITLAB_PRIVATE_TOKEN` / `DATAHUB_GMS_TOKEN`**；使用本机环境变量或已 `.gitignore` 的本地配置文件，并定期轮换 token。
