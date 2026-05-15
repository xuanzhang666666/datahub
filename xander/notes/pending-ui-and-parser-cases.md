# 待办 Case 记录（暂不实施）

记录时间：2026-05-15

## 1. Etl Script（RichText）展开后无法滚到底

**现象**：Dataset 侧栏 Summary 区（`SidebarStructuredProperties`）中，**Etl Script** 为 full text / Markdown 代码块；内容较长时点击 **Show more** 展开，底部脚本被裁切，无法在区块内继续向下滚动。

**环境**：neo4j2 DataHub `acryldata/datahub-frontend-react:v1.5.0.4`（与 GMS 同版本）。

**根因（已分析，未合入）**：

- `CompactMarkdownViewer` 展开后无 `max-height`，长内容撑破父级滚动区域。
- 侧栏 `EntityProfileSidebar` 的 `Content` 曾设 `white-space: nowrap`，多行代码/富文本布局异常。

**拟定改法**（本地曾有草稿，未提交、未打镜像）：

| 文件 | 改动要点 |
|------|----------|
| `datahub-web-react/.../CompactMarkdownViewer.tsx` | 展开后 `max-height: min(60vh, 480px)` + `overflow-y: auto` |
| `datahub-web-react/.../StructuredPropertyValue.tsx` | 查看器 `min-width: 0; width: 100%` |
| `datahub-web-react/.../SidebarStructuredProperties.tsx` | `PropertyValueWrapper` 强制 `white-space: normal` |
| `datahub-web-react/.../EntityProfileSidebar.tsx` | 去掉侧栏 `Content` 的 `nowrap` |
| `datahub-web-react/.../SidebarSection.tsx` | `ContentBody` 增加 `min-width: 0` |

**上线注意**：

- 仅 `docker compose restart datahub-frontend-react` **不会**带上 UI 修复，需从源码构建自定义 `datahub-frontend-react` 镜像并替换 compose 中的 `DATAHUB_FRONTEND_IMAGE`（或等价发布流程）。
- 重建/替换前端 **不影响** MySQL / ES / GMS 元数据。

**验收**：大 ETL 作业对应表 → 侧栏展开 Etl Script → Show more → 代码块内可滚至末尾。

---

## 2. shell 行 `${dt}` 污染 job_path（`dim_sku_store_day_info_v1_di`）

**现象**：`w-run-task.sh dim_sku_store/day_info_v1_di ${dt} ${dt}` 被解析成带 `${dt}` 的 `job_file_name`，GitLab 拉 ETL 404，batch/debug 在 ETL 阶段失败。

**拟定改法**：`runtime_parser` 将 `${var}` / `$var` 视为 runner 尾部参数，并在 `${` 处截断 `rest`（`badcases` id: `shell_trailing_braced_vars_dt`）。

**状态**：代码草稿在本地，**未**打包部署 neo4j2。

---

## 操作记录

- 2026-05-15：neo4j2 已执行 `docker compose restart datahub-frontend-react`（仅重启官方 v1.5.0.4 镜像，**不含**上述 UI 修改）。
