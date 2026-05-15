# xander 归档（deprecated）

本目录存放**不再随 Jenkins 日常任务发布**的旧脚本、实验工具与已迁出的单测，仅作历史参考。

| 子目录 | 说明 |
| --- | --- |
| `deprecated-run/` | 旧按库 HMS 入仓、dim 导出、bastion 示例、分区统计等 shell/py/sql |
| `deprecated-python/` | 根目录独立 Python 工具（pilot、显式血缘 emit、partition stats 等） |
| `deprecated-scripts/` | 原 `xander/scripts/gms-es/`（GMS/ES 调试 JSON、示例） |
| `deprecated-docs/`、`deprecated-notes/`、`deprecated-infra/`、`deprecated-hooks/` | 旧文档、笔记、compose 片段、可选 hook |
| `job_info_sync_datahub_tests/` | 从包内迁出的单元测试（恢复时需拷回 `python/job_info_sync_datahub/tests/`） |

当前线上 **Jenkins 仅保留**两条链路，见仓库根下 [`../README.md`](../README.md)。
