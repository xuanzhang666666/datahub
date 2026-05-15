# DataHub 入仓规范（约束清单）

本文档用于约束后续向 DataHub 写入元数据的方式，避免出现 **Browse 下重复目录分支**、**ES 与主存（GMS）不一致** 等问题。适用于 Dataset（尤其 Hive）、Container 及相关 ingestion / 运维操作。

---

## 1. 目标与原则

| 原则 | 说明 |
| --- | --- |
| **路径单一真相** | 同一实体在索引中的 **`browsePathV2`（及容器层级）只能有一种规范形态**，禁止长期并存「默认推导路径」与「带 Instance/Catalog 的完整路径」。 |
| **主存为准** | 权威数据在 GMS 持久化层；**Elasticsearch 仅为主存的投影**，不一致时修复索引而非随意删实体。 |
| **少源入仓** | 同一环境、同一类实体尽量由 **一条确定的 ingestion 管线** 写入；多源时必须对齐本文档的路径与 URN 规则。 |

---

## 2. Dataset（Hive）规范

### 2.1 `browsePathsV2`（强制）

- **必须**包含 **`DataPlatformInstance`** 段（URN 形态，例如 `urn:li:dataPlatformInstance:(urn:li:dataPlatform:hive,<instance-id>)`），**不得**仅使用平台名字符串（如单独的 `blf-prod-hive`）作为与 instance 等价的 browse 根。
- **推荐**完整链：**Instance → Catalog（如 `hive`）→ Schema（database）→ …**，与 Hive Metastore / 业务库层级一致，避免根下再出现无名或歧义分叉。
- **禁止**依赖「仅按 `database.table` 或 FQDN 用 `.` 切分」生成的默认路径作为唯一 browse 信息长期入索引；若中间态存在，须在上线或修复窗口内 **刷成规范路径**（见第 5 节）。

### 2.2 URN 与平台实例

- **Dataset URN**、**DataPlatformInstance** 与调度/集群命名保持 **稳定、可映射**；实例 ID 变更须有迁移与重索引计划。
- 同一物理表 **只对应一个** 逻辑 Dataset URN；避免重复注册（多 URN 指向同一表）导致 browse 与血缘分裂。

### 2.3 其他 Dataset 方面

- **Schema、分区、属性** 等 aspect 与官方模型字段对齐；自定义字段不破坏标准 browse / 搜索字段的含义。
- 大批量 ingestion 使用 **幂等** 配置（可重复跑、可断点），避免半成功状态长期残留。

---

## 3. Container 规范

- **父子关系** 必须唯一、无环：通过 **`Container` aspect** 与 **`parentContainer`** 等与模型一致的字段表达，避免同一逻辑容器在 ES 中出现多套不一致的 path 前缀。
- **Browse 用的容器路径** 与 **Dataset 的 `browsePathV2`** 在语义上衔接（同一 catalog/schema 命名空间一致），避免同一 schema 在「容器树」与「数据集树」下显示为两个无关分支。
- 不在无评审的情况下 **批量删除 Container 实体** 以「整理目录」；优先修正关系与索引。

---

## 4. 入仓流程约束

1. **新接数据源**：在 recipe / 代码中明确 **browse 规则与 Instance**，并在测试 GMS + ES 上验证 **Browse 单分支** 后再扩到生产。
2. **变更 connector 或路径逻辑**：视为 **破坏性变更**，需评估存量索引；必要时安排 **`restoreIndices`（`browsePathsV2`，按需 `container`）** 或按 URN 批量修复。
3. **禁止**为消除重复 browse 文件夹而 **直接删除 Dataset/Container 实体**「救火」——会破坏血缘与审计；应先确认 **主存 path 是否正确**，再修索引与消费链路。

---

## 5. 索引与健康（运维必查）

| 项 | 要求 |
| --- | --- |
| **MAE Consumer** | 保持正常运行；关注 **Kafka 堆积、消费错误**；大促或大批量 restore 后加强巡检。 |
| **主存 vs ES** | 升级、重建索引、大批量修复后，对 **`browsePathV2` 是否含 `dataPlatformInstance`** 等做 **抽样或聚合校验**（可与现有 ES 查询脚本对齐）。 |
| **修复手段** | 主存正确、ES 陈旧时：使用 **`restoreIndices`**（GET 按 `urnLike` / `aspectName` 或 POST 指定 URN 集合），避免手写 PATCH ES。 |

参考脚本目录（示例与查询体）：`xander/scripts/gms-es/`（如 `es_count_short_browsepath.json`、`urns_hive_short_browsepath_batch.json`、`neo4j2_restore_short_browsepath_batch.example.sh`）。

---

## 6. 上线前检查清单（简版）

- [ ] Hive（或等价）Dataset 的 **`browsePathsV2` 含 Instance URN**，且 catalog/schema 层级符合约定。  
- [ ] **Browse V2 根下** 对同一 platform instance **不出现两个同名父节点**（无「纯字符串」与「URN 桶」并存）。  
- [ ] Container 父子与 Dataset browse **命名空间一致**。  
- [ ] 已确认 **MAE 消费正常**，或对刚跑完的 restore 预留 **索引最终一致** 的等待窗口并复验 ES。  
- [ ] 未使用「删实体」作为修复 browse 的首选手段。

---

## 7. 修订

规范随 DataHub 版本与内部平台命名调整而更新；变更时在本节记录日期与摘要。

| 日期 | 摘要 |
| --- | --- |
| 2026-05-11 | 初版（Hive `browsePathV2` / ES 漂移）；同日将 `xander/` 整理为 `run/`、`docs/`、`python/`、`notes/`、`infra/`、`scripts/gms-es/` 并同步文中脚本路径。 |
