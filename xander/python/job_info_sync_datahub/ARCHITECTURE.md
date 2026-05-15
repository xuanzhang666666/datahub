# job_info_sync_datahub — 逻辑梳理

面向「调度作业 → DataHub 血缘 / 属性」流水线；与官方 DataHub 仓库其他模块独立。

## 数据流（主链路）

1. **DMP / 元数据**  
   `schedule_client` 等拉取 `shell_command`、`job_display_name` 等。

2. **运行时解析**（`runtime_parser`）  
   - 从 `shell_command` 识别 `…/<gitlab_name>/bin/(w|bike|yummy)-run-task.sh`。  
   - `extract_gitlab_name` → 仓库别名；`GITLAB_NAME_TO_PROJECT_PATH` / localfolder 决定 GitLab `group/project`。  
   - `extract_job_path_and_type` → `(job_path, job|python)`：runner 后的路径段；在 **`prod`/`before`/`after`、单字母分区、`--flag`、管道 `|`** 等处截断；对 **`$VAR`/`${VAR}`/`$1`** 先用正则 **`_strip_shell_var_suffix`** 截断（解决路径与变量**无空白粘连**、**多空格**、**NBSP 等非常规空白** 导致 `split()` 拆不出独立 `$` token 的问题），再经 **`_cut_rest_at_runtime_tokens`** 处理其余调度 token。  
   - `job_file_name` → `jobs/.../<base>.job` 或 `.py` 候选路径。  
   - `parse_runtime_context` 汇总为 `RuntimeContext`。

3. **ETL 脚本来源**（`etl_file_resolver` + `gitlab_client`）  
   GitLab 与 `BLF_ETL_LOCAL_ROOT` 双源合并，解析出 `etl_content`。

4. **SQL / 血缘解析**（`lineage_parser`、`lineage_vote` / LLM 等）  
   产出 `TableLineage`、`FieldLineage` 等。

5. **写入 DataHub**（`datahub_writer`）  
   structuredProperties（OpenAPI PATCH）与 `upstreamLineage`（SDK MCP）。

## 独立子能力：手动表级上游（`manual_upstream_lineage`）

- **入口**：`python -m job_info_sync_datahub.manual_upstream_lineage` 或 `run_add_manual_upstream_lineage.sh`。  
- **输入**：下游 / 上游 **必须为 `库.表`**（`parse_fqtn`）。  
- **行为**：`DataHubGraph.get_aspect` 读已有 `upstreamLineage`，与新区段 **合并**（URN 去重），保留 `fineGrainedLineages`；`--replace` 仅保留一条；已存在则跳过。  
- **写入**：`MetadataChangeProposalWrapper` + `DatahubRestEmitter.emit_mcp`（依赖 `acryl-datahub`）。

## 修改后必跑测试（再上传 neo4j2 / 发版）

```bash
cd xander/python
sh scripts/run_job_info_sync_datahub_tests.sh
```

- **默认**：只跑 `test_runtime_parser` + `test_manual_upstream_lineage`（不依赖 **trino**，适合作为上传前门禁）。  
- **全量**：`FULL_TESTS=1` 时需能 `import trino`，并跑 `job_info_sync_datahub/tests/` 下全部用例（与 Jenkins 批量血缘环境一致）。

脚本依赖 **pytest**。`manual_upstream_lineage` 中与 GMS 合并相关的用例在未安装 **acryl-datahub** 时会 **skip**。失败则**不要** `put2` 上传包。

详见 **`TESTING.md`** 与 **`ARCHITECTURE.md`**。
