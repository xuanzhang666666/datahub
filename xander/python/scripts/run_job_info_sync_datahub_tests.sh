#!/usr/bin/env bash
# run_job_info_sync_datahub_tests.sh — 修改 job_info_sync_datahub 后先跑本脚本，通过后再上传 neo4j2
#
# 默认：仅跑「上传最低门禁」（不依赖 trino / DMP）：runtime_parser + manual_upstream_lineage
# 全量：FULL_TESTS=1 且当前 Python 可 import trino 时跑 job_info_sync_datahub/tests/ 下全部用例
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$ROOT"
cd "$ROOT"

if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
else
  PYTHON=python3
fi

if ! "$PYTHON" -c "import pytest" 2>/dev/null; then
  echo "ERROR: pytest not found (python=${PYTHON})." >&2
  echo "  Run: ${PYTHON} -m pip install pytest" >&2
  echo "  Or: export LINEAGE_PYTHON=/path/to/python-with-pytest" >&2
  exit 1
fi

echo "==================================================================="
echo " job_info_sync_datahub tests"
echo " PYTHON=$PYTHON  PYTHONPATH=$ROOT  FULL_TESTS=${FULL_TESTS:-0}"
echo "==================================================================="

if [[ "${FULL_TESTS:-0}" == "1" ]]; then
  if ! "$PYTHON" -c "import trino" 2>/dev/null; then
    echo "ERROR: FULL_TESTS=1 需要已安装 trino（与 batch_sync 一致）。" >&2
    exit 1
  fi
  echo "[INFO] FULL_TESTS=1 → 运行全部 job_info_sync_datahub/tests/"
  "$PYTHON" -m pytest job_info_sync_datahub/tests/ -q "$@"
else
  echo "[INFO] 默认门禁 → runtime_parser + manual_upstream + hive_single_table_ingest + two_stage_tmp"
  "$PYTHON" -m pytest \
    job_info_sync_datahub/tests/test_runtime_parser.py \
    job_info_sync_datahub/tests/test_manual_upstream_lineage.py \
    job_info_sync_datahub/tests/test_hive_single_table_ingest.py \
    job_info_sync_datahub/tests/test_two_stage_tmp_lineage_contract.py \
    -q "$@"
fi

echo "[DONE] tests passed"
