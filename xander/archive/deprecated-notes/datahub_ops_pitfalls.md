# DataHub 运维踩坑记录（neo4j2 / BLF）

本文记录一次「Navigate 双分叉 + delete 删不掉」的排查结论与**固定操作**，避免重复踩坑。

---

## 1. Navigate 里同一 Hive 库出现两条目录分支

### 现象

在 **Hive** 平台下，同一实例下出现类似：

- `blf-prod-hive → hive → data_drink`
- `blf-prod-hive → data_drink`（少一层 `hive`）

条目数相加约等于表+视图总数。

### 原因（概念）

- **BrowsePathsV2** 有两类来源：按 **容器链**（Catalog `hive` → Schema `data_drink`）生成，或在边界情况下按 **dataset 名字按 `.` 拆分** 生成兜底路径。
- 当 recipe 配置了 **`platform_instance`** 时，dataset 名里常带 `blf-prod-hive.data_drink.xxx` 前缀；兜底路径的前两段会变成 **`blf-prod-hive` → `data_drink`**，与「容器链」在 UI 上看起来像两套目录。
- 单次 ingest、甚至「全新库」也可能出现，与 **异步 sink、workunit 批次顺序** 等有关，不单是「跑了两次历史任务」。

### 当前策略（单集群 Hive）

- **只有一套生产 Hive**：recipe **不写** `platform_instance`，URN 为 `data_drink.table,PROD`，目录更干净、分叉概率低。
- **若将来有多集群**：再启用 `platform_instance`，并查阅官方/社区对 **browsePathsV2 + 多实例** 的推荐配置（例如 catalog/database 命名与实例对齐），单独开文档，勿与单集群 recipe 混用。

---

## 2. `datahub delete` 在 neo4j2 上执行后「页面还在」

### 根因（本次已证实）

在 **neo4j2 宿主机** 上使用：

```bash
export DATAHUB_GMS_URL=http://127.0.0.1:8080
datahub delete ...
```

时，**宿主机 8080 未必是 GMS**。常见 compose 里 **前端** 也监听 8080；CLI 若连到前端，delete/graph 请求会**走错服务**，表现为：命令很快结束、无报错或交互异常，但 **dry-run 仍显示大量实体**，页面数据仍在。

### 正确做法（推荐、可自动化）

在 **与 GMS 同一 Docker 网络** 的容器里执行 CLI，并指向 **容器名**：

```bash
docker exec root-datahub-actions-1 \
  env DATAHUB_GMS_URL=http://datahub-gms:8080 \
  datahub delete --platform hive --env PROD --hard --force
```

- 删除前可用 `--dry-run` 确认数量。
- `--force` 跳过交互确认（脚本/自动化用）。

### 若必须在宿主机执行

先确认 **GMS 在宿主机上的真实端口**（例如 `docker ps` 看 `datahub-gms` 的 `0.0.0.0:????->8080/tcp`），再把 `DATAHUB_GMS_URL` 设为 `http://127.0.0.1:<GMS映射端口>`。**不要用「猜测的 8080」**。

---

## 3. 标准操作流程（换 recipe / 清 Hive 元数据）

1. **确认** `DATAHUB_GMS_URL` 指向 **GMS**（见上文）。
2. **dry-run**：`datahub delete --platform hive --env PROD --hard --dry-run`
3. **hard delete**：加 `--force` 去掉 `--dry-run`。
4. **再次 dry-run**：应出现 `Found no urns to delete`（或等价提示）。
5. **重新 ingest**：Jenkins 或 `ingest_hive_database_to_datahub.sh <库名>`。
6. 若 UI 仍偶发陈旧：可重启 `datahub-gms` / `datahub-frontend-react`；**不能**替代「连对 GMS 的 delete」。

---

## 4. 辅助脚本（可选）

仓库内 **`xander/run/delete_hive_entities.py`**：用 Graph SDK 枚举 hive 的 dataset/container 并 hard delete，适合在**能直连 GMS** 的环境执行。堡垒机若禁止任意命令，仍以 **docker exec + datahub CLI** 为准。

---

## 5. 检查清单（以后自查）

| 检查项 | 说明 |
| --- | --- |
| GMS 地址 | `127.0.0.1:8080` 是否是 GMS？若不是，delete/ingest 行为均可能异常 |
| delete 后验证 | 必须再跑一次 `--dry-run`，确认 `Found no urns` |
| 单集群 recipe | 不写 `platform_instance`，减少 Navigate 分叉 |
| 自动化删除 | 使用 `docker exec ... datahub-gms:8080`，避免宿主机端口误判 |
