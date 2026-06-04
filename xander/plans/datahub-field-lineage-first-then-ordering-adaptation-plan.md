# DataHub 字段级血缘先行与订货算法适配计划

## Summary

本阶段先在 DataHub 上建立可用、可信、可排查的 Hive 字段级血缘，再基于字段级血缘图解决订货算法模型适配 B 公司数据仓库的问题。

整体顺序：

```text
阶段 1：A 侧 DataHub 字段级血缘建设
阶段 2：基于 DataHub 字段血缘追溯算法用表字段
阶段 3：产出 A 侧字段需求契约
阶段 4：拿契约与 B 侧数据仓库做字段/口径适配
```

目标不是“DataHub 图上有线”，而是让字段血缘能支撑：

- 字段来源追溯
- 字段加工逻辑解释
- 订货算法输入字段契约
- A/B 数据仓库模型差异适配
- 后续影响分析与数据质量排查

## Key Principles

- 只基于生产写入 SQL 建字段血缘，不用普通 `SELECT` 查询建血缘。
- 字段血缘建设必须使用 DataHub structured properties 中的：
  - `blf.data.warehouse.etl_script`
  - `blf.data.schedule.execute_shell`
  - `blf.data.schedule.schedule_url`
- 解析 ETL 前必须先用 `execute_shell` 和脚本内变量赋值替换 ETL 脚本。
- `.job` shell 脚本只保留 `<表名>_run` 入口函数可达的函数，避免无关 SQL 干扰 LLM。
- Python ETL 保留完整脚本，按 Python 初始化和执行顺序分析。
- 字段血缘入图前必须经过 Excel 人工审核。
- Data Availability Flag 已包含 `字段血缘` 的表视为已确认，默认禁止覆盖修改。
- 所有中间结果必须落盘，方便排查变量替换、函数裁剪、LLM 输入与审核结果。

## Phase 1: 建立 DataHub 字段级血缘

### 1. 输入范围

优先选择算法、核心维表、核心明细表相关的 Hive 表。

Jenkins 参数使用：

```text
TABLES
```

格式为 Multi-line String，一行一个 `库.表`：

```text
default.dim_store_info
default.dim_sku_info
default.dw_order_sku_promotion_v1_di
```

### 2. 导出字段血缘候选 Excel

使用脚本：

```bash
/data/datahub/scripts/run_field_lineage_export_to_excel.sh
```

导出流程：

```text
读取 TABLES
→ 生成 DataHub dataset URN
→ 读取 structuredProperties
→ 获取 ETL Script / Execute Shell
→ 解析 Execute Shell 变量
→ 解析 ETL Script 内部简单变量赋值
→ 替换 ETL Script 中的 ${VAR} / $VAR / {{ VAR }}
→ .job 脚本按 <表名>_run 入口函数裁剪
→ Python 脚本保留完整脚本
→ 将最终脚本发送给 LLM
→ 生成候选字段血缘 Excel
```

默认输出：

```text
/data/datahub/out/field_lineage_export/{BATCH_CODE}/{BATCH_CODE}_{库.表}.xlsx
```

Jenkins workspace 中间产物：

```text
$WORKSPACE/field_lineage_debug/{BATCH_CODE}/{库.表}/
├── execute_shell.txt
├── original_etl_script.sql 或 original_etl_script.py
├── runtime_variables.json
├── resolved_etl_script.sql 或 resolved_etl_script.py
├── llm_input_etl_script.sql 或 llm_input_etl_script.py
└── processing_summary.json
```

这些中间产物用于排查：

- 变量是否替换成功
- `${TABLE_NAME}` / `${UNIQ_KEY}` / `$DATE` 是否还残留
- `.job` 中哪些函数被保留
- 哪些无关函数被移除
- 最终发给 LLM 的脚本是什么

### 3. 人工审核 Excel

Excel 核心 sheet：

```text
candidate_lineage
unresolved_fields
source_context
```

`candidate_lineage` 中重点审核：

- `target_table`
- `target_field`
- `source_table`
- `source_field`
- `transform_expression`
- `transform_explanation`
- `evidence_sql`
- `confidence`
- `review_status`

审核规则：

- 确认无误的行设置为 `APPROVED`。
- 不确定的行保持 `PENDING` 或改为 `NEEDS_FIX`。
- 明显错误的行改为 `REJECTED`。
- 不能为了覆盖字段而编造来源。
- 多来源字段必须拆成多行或明确多来源，例如 `coalesce(a, b)`。
- `transform_expression` 必须是真实 SQL 表达式。
- `transform_explanation` 必须能让业务和算法同学理解字段含义与加工逻辑。

### 4. 导入审核后的字段血缘到 DataHub

使用脚本：

```bash
/data/datahub/scripts/run_field_lineage_import_to_datahub.sh
```

Jenkins 参数：

```text
BATCH_CODE
TABLES
```

导入流程：

```text
读取 BATCH_CODE 和 TABLES
→ 找到对应 Excel
→ 只读取 review_status=APPROVED 的行
→ 按 target_table + target_field 聚合多来源字段
→ 生成 DataHub FineGrainedLineage
→ 写入 upstreamLineage.fineGrainedLineages
```

默认行为：

```bash
FIELD_LINEAGE_CLEAR_EXISTING=1
```

表示导入前清空该表已有 `fineGrainedLineages`，然后写入本次审核结果。

如果需要合并已有字段血缘：

```bash
FIELD_LINEAGE_CLEAR_EXISTING=0
```

保护规则：

```text
如果 Data Availability Flag 已包含 字段血缘，则认为该表字段血缘已确认，导入脚本禁止修改。
```

### 5. DataHub 入图内容

写入 Dataset 的：

```text
upstreamLineage.fineGrainedLineages
```

每条字段血缘应包含：

```text
upstreams: 上游 schemaField URN 列表
downstreams: 下游 schemaField URN 列表
transformOperation: 中文解释 + SQL 表达式
confidenceScore: 置信度
```

示例：

```text
default.mid_store_info_bach.store_name
default.static_mid_store_info_hd.store_name
→ default.dim_store_info.store_name

transformOperation:
/* 中文解释：优先取 default.mid_store_info_bach 表的 store_name，为空时取 default.static_mid_store_info_hd 表的 store_name，表示门店名称按优先级兜底合并。 */
regexp_replace(coalesce(bach.store_name, hd.store_name), '''', '')
```

## Phase 1 Acceptance Criteria

每张完成字段血缘建设的表必须满足：

- DataHub Dataset 上存在 `upstreamLineage.fineGrainedLineages`。
- 字段血缘覆盖所有关键非分区字段。
- 分区字段如 `dt` 可不强制入图，但报告中要说明来源通常是调度日期。
- 下游字段必须是真实字段名，不能是 `col_1` / `col_2`。
- 上游字段必须是真实字段 URN，不能出现 `field_a%2C field_b` 这种拼接错误。
- `transformOperation` 不是空值，也不是泛泛的“字段加工逻辑 1”。
- Excel 有人工审核记录。
- debug 中间产物存在，可追溯变量替换和 LLM 输入。
- Data Availability Flag 可在确认后标记 `字段血缘`。

## Phase 1 Quality Checks

### 1. 字段覆盖检查

对比：

```text
schemaMetadata.fields
fineGrainedLineages.downstreams
```

输出：

```text
已覆盖字段
未覆盖字段
多余字段
异常字段 URN
```

### 2. 上游字段合法性检查

检查是否存在：

```text
source_field 包含逗号
source_field 包含 %2C
source_field 为空
source_table 不存在
downstream 字段不在目标 schema
```

### 3. 加工逻辑检查

检查：

```text
transformOperation 为空
transformOperation 只有中文无 SQL
transformOperation 只有 SQL 无解释
confidenceScore 全部无差别为 1.0
```

### 4. 已确认保护检查

如果 `Data Availability Flag` 已包含：

```text
字段血缘
```

则导入任务不得覆盖该表字段血缘。

## Phase 2: 基于 DataHub 字段血缘追溯订货算法 A 侧字段

### 1. 输入

你提供约 20 张订货算法使用表。

约定：

```text
这些表里的所有字段都视为订货算法已使用字段。
```

输入格式：

```text
库.表
```

未带库名时按：

```text
default.<表名>
```

### 2. 追溯策略

优先查 DataHub 图：

```text
目标表字段
→ upstreamLineage.fineGrainedLineages
→ 上游字段
→ 递归继续向上游追溯
```

不优先重新解析 SQL。

只有当 DataHub 字段血缘缺失、不完整、或有明显异常时，才回退到：

```text
structuredProperties
→ Execute Shell
→ ETL Script
→ 变量替换
→ 静态解析 / LLM 辅助
→ open_questions
```

### 3. 输出

产出 A 侧字段需求契约：

```text
field_contract.xlsx
field_lineage.json
lineage_report.md
open_questions.md
```

`field_contract.xlsx` 至少包含：

- 算法目标表
- 算法目标字段
- 字段中文含义
- 直接上游表
- 直接上游字段
- 最终源头表
- 最终源头字段
- 加工表达式
- 加工说明
- 是否必需
- 是否可由 B 侧提供
- B 侧候选表
- B 侧候选字段
- 适配状态
- 待确认问题

## Phase 2 Acceptance Criteria

- 每个算法使用字段都有来源链路或明确失败原因。
- 字段链路优先来自 DataHub 字段级血缘。
- 对血缘缺失字段，必须列入 `open_questions.md`。
- 不编造 B 侧映射。
- 不因为某个字段解析失败而中断整批任务。
- 报告中能区分：
  - DataHub 图谱确认
  - SQL / LLM 回退推断
  - 人工待确认

## Phase 3: B 公司数据仓库适配

当 B 公司数据仓库信息可用后，再进入适配阶段。

### 1. B 侧输入

可能输入：

- B 侧表清单
- B 侧字段清单
- B 侧 DDL
- B 侧业务口径文档
- B 侧样例数据
- B 侧数据负责人确认结果

### 2. 映射方法

用 A 侧字段需求契约作为标准，而不是直接用 A 侧物理表结构。

映射粒度：

```text
A 算法字段需求
→ A 源字段与加工逻辑
→ 抽象业务口径
→ B 侧候选字段
→ B 侧适配 SQL / 口径调整
```

### 3. B 侧输出

输出：

```text
b_side_mapping.xlsx
adaptation_sql.sql
adaptation_gap_report.md
open_questions_for_b.md
```

重点回答：

- 哪些字段 B 侧可以直接提供
- 哪些字段需要加工
- 哪些字段需要多表 join
- 哪些字段 B 侧没有
- 哪些字段口径不同
- 哪些字段需要业务确认

## Risks

- 字段血缘如果由 LLM 生成但未审核，会污染 DataHub 图谱。
- `.job` 脚本变量、外部 `source` 文件、动态 SQL 可能导致替换不完整。
- Python ETL 动态拼 SQL 时，LLM 可能漏掉初始化逻辑或运行路径。
- `select *` 依赖 Hive schema 展开，否则字段顺序容易错。
- 已确认的字段血缘被覆盖会破坏后续适配结果。
- A 侧字段来源清楚，不代表 B 侧一定有同口径数据。

## Recommended Jenkins Jobs

### Job 1: 导出字段血缘候选

参数：

```text
TABLES
LLM_PROVIDER
LLM_MODEL
FIELD_LINEAGE_CONCURRENCY
FIELD_LINEAGE_RETRY_COUNT
```

执行：

```bash
sh /data/datahub/scripts/run_field_lineage_export_to_excel.sh
```

### Job 2: 导入审核后的字段血缘

参数：

```text
BATCH_CODE
TABLES
FIELD_LINEAGE_CLEAR_EXISTING
DRY_RUN
```

执行：

```bash
sh /data/datahub/scripts/run_field_lineage_import_to_datahub.sh
```

### Job 3: 字段血缘质量检查

后续建议新增，检查：

```text
字段覆盖率
异常 schemaField URN
transformOperation 完整性
Data Availability Flag 状态
```

### Job 4: 订货算法字段契约生成

后续建议新增，输入约 20 张算法使用表，输出 A 侧字段需求契约。

## Current Implementation Status

已完成：

- 字段血缘 Excel 导出脚本。
- 字段血缘 Excel 导入脚本。
- DataHub token 支持。
- LLM_PROVIDER / LLM_MODEL 支持。
- Execute Shell 与脚本内简单变量替换。
- `.job` 入口函数裁剪。
- Python ETL 完整发送给 LLM。
- debug 中间产物落盘到 Jenkins workspace。
- Data Availability Flag 已包含 `字段血缘` 时禁止覆盖。

待补充：

- 字段血缘质量审计 Jenkins Job。
- 基于 DataHub 字段血缘图的订货算法字段契约生成 Job。
- B 侧字段映射工作流。
- 字段血缘确认后自动/半自动更新 Data Availability Flag 的流程。

## Assumptions

- A 侧 Hive 表 schema 已同步到 DataHub。
- 目标表 structuredProperties 已包含 ETL Script 与 Execute Shell。
- 字段血缘候选必须经过人工审核后才能写入 DataHub。
- B 公司数据仓库当前不可用，本计划先完成 A 侧字段血缘与字段契约准备。
- 后续 B 侧适配以字段契约为准，不直接照搬 A 侧物理模型。
