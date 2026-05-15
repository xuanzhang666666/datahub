# Hive 表清单 xlsx（输入）

与线上 **`/data/datahub/in/`** 约定对应：放 **按分区/导出日期命名** 的 Hive 元表清单 Excel（如 `20260512-hive-tables.xlsx`），供本地或文档中引用。

本地跑串行 ingest 示例：

```bash
export HIVE_META_DT=20260512
export HIVE_XLSX_IN="$(git rev-parse --show-toplevel)/xander/in/${HIVE_META_DT}-hive-tables.xlsx"
export HIVE_XLSX_CHUNK_DIR="/tmp/xlsx_chunks_${HIVE_META_DT}"
# …其余与 Jenkins 任务 2 相同
```

若仓库策略不希望提交大 xlsx，可将 `*.xlsx` 加入 `xander/.gitignore`，仅保留本说明与本目录占位。
