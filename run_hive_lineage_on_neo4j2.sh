#!/usr/bin/env bash
# 兼容旧路径：转发到统一的 Hive 库入仓脚本（参数为库名，如 default、data_logistics）。
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/xander/run/ingest_hive_database_to_datahub.sh" "$@"
