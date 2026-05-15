# job_info_sync_datahub — Bad Cases

批量血缘 / 脚本拉取 / LLM 解析过程中发现的**非典型** `shell_command` 或脚本形态。  
先登记在 [`cases.json`](cases.json)，后续统一改 `runtime_parser`、提示词或规则时再批量处理。

## 字段说明

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定标识，便于引用与单测 |
| `component` | 受影响模块（如 `runtime_parser`） |
| `status` | `open` / `fixed` / `wontfix` |
| `symptom` | 线上现象（日志、SKIP/FAIL 类型） |
| `fix_hint` | 建议修复方向（实现时再落地） |
| `category` | 可选分类（如 `etl_source_unavailable_on_gitlab`） |
| `alternate_source` | 可选；GitLab 不可用时的备用取数系统（`TBD` 表示待对接） |

## 新增方式

在 `cases.json` 的 `cases` 数组末尾追加一条 JSON 对象，保持 `id` 唯一。
