#!/usr/bin/env bash
# 兼容旧路径：按库名 HMS 入仓脚本已迁至 xander/archive（新任务请用 xlsx 串行 ingest）。
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/xander/archive/deprecated-run/ingest_hive_database_to_datahub.sh" "$@"
