#!/usr/bin/env bash
# run_batch_lineage_sync.sh — 批量血缘同步，直接在 Jenkins slave 上运行（无需 Docker）
set -euo pipefail

# ── 参数（环境变量）───────────────────────────────────────────────────────────
# JOB_INFO_SYNC_DIR   Python 包目录（内含 batch_sync.py），默认与脚本同级：$SCRIPT_DIR/job_info_sync_datahub
# LINEAGE_REPORT_DIR  报告目录；默认 $WORKSPACE/lineage_reports，无 WORKSPACE 时为 $SCRIPT_DIR/lineage_reports
PREFIX="${PREFIX:-}"
CONCURRENCY="${CONCURRENCY:-10}"
DRY_RUN="${DRY_RUN:-0}"
LINEAGE_VOTE="${LINEAGE_VOTE:-1}"
LLM_TIMEOUT="${LLM_TIMEOUT:-90}"
JOB_FILE="${JOB_FILE:-}"
RETRY_FAILED="${RETRY_FAILED:-0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 代码目录：默认与脚本同级（如 /data/datahub/scripts/job_info_sync_datahub），不放 Jenkins workspace
PKG_DIR="${JOB_INFO_SYNC_DIR:-$SCRIPT_DIR/job_info_sync_datahub}"
PYTHONPATH_ROOT="$(cd "$(dirname "$PKG_DIR")" && pwd)"
# 报告默认可写 workspace 便于归档；未设 WORKSPACE 时用脚本目录
if [[ -n "${LINEAGE_REPORT_DIR:-}" ]]; then
  REPORT_DIR="$LINEAGE_REPORT_DIR"
elif [[ -n "${WORKSPACE:-}" ]]; then
  REPORT_DIR="$WORKSPACE/lineage_reports"
else
  REPORT_DIR="$SCRIPT_DIR/lineage_reports"
fi

# 解释器：LINEAGE_PYTHON > /opt/anaconda3 > 仅 root 时用 /root/anaconda3 > 系统 python3
# 注意：Jenkins 常以 iuser 运行，无法读 /root/anaconda3 下 site-packages，勿对非 root 默认选 /root/...
if [[ -n "${LINEAGE_PYTHON:-}" ]]; then
  PYTHON="$LINEAGE_PYTHON"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  PYTHON=/opt/anaconda3/bin/python
elif [[ "$(id -u)" -eq 0 ]] && [[ -x /root/anaconda3/bin/python ]]; then
  PYTHON=/root/anaconda3/bin/python
else
  PYTHON=python3
fi

mkdir -p "$REPORT_DIR"

echo "==================================================================="
echo " Batch lineage sync started"
echo " date=$(date -Iseconds)"
echo " PYTHON=$PYTHON"
echo " PKG_DIR=$PKG_DIR  PYTHONPATH_ROOT=$PYTHONPATH_ROOT"
echo " REPORT_DIR=$REPORT_DIR"
echo " PREFIX=$PREFIX  CONCURRENCY=$CONCURRENCY  DRY_RUN=$DRY_RUN"
echo " LINEAGE_VOTE=$LINEAGE_VOTE  LLM_TIMEOUT=$LLM_TIMEOUT"
echo " Each finished job prints one line: [PROGRESS] ... (see Jenkins console)"
echo "==================================================================="

# ── 加载 env 文件 ─────────────────────────────────────────────────────────────
for _cand in "${LINEAGE_ENV_FILE:-}" "$SCRIPT_DIR/lineage.env" ${WORKSPACE:+"$WORKSPACE/lineage.env"}; do
  [[ -z "$_cand" ]] && continue
  if [[ -r "$_cand" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$_cand"; set +a
    echo "[INFO] loaded env: $_cand"
    break
  fi
done

# ── 依赖自检（不在此自动 pip：避免误用系统 3.6 / 错误 index；由你在目标环境里装好）────────
if ! "$PYTHON" -c "import trino, sqlglot, openpyxl; from datahub.emitter.rest_emitter import DatahubRestEmitter" 2>/dev/null; then
  echo "ERROR: 依赖 import 失败（解释器: $PYTHON，用户: $(id -un)）。" >&2
  if [[ "$PYTHON" == /root/* ]] && [[ "$(id -u)" -ne 0 ]]; then
    echo "Jenkins 以非 root 运行时，通常不能读取 /root 下 Anaconda 的 site-packages，与是否已 pip install 无关。" >&2
    echo "请任选其一：" >&2
    echo "  1) root 执行: sudo cp -a /root/anaconda3 /opt/anaconda3 && sudo chmod -R o+rX /opt/anaconda3" >&2
    echo "     Jenkins 里: export LINEAGE_PYTHON=/opt/anaconda3/bin/python" >&2
    echo "  2) 用当前 Jenkins 用户在其 HOME 安装 Miniconda，再 pip install 上述包，并 export LINEAGE_PYTHON=.../bin/python" >&2
    echo "  3) 若策略允许，本 Job 改为以 root 执行。" >&2
  else
    echo "请在该环境中执行：" >&2
    echo "  $PYTHON -m pip install -U trino sqlglot openpyxl 'acryl-datahub>=0.12'" >&2
    echo "或设置 LINEAGE_PYTHON 指向已安装上述包的 Python。" >&2
  fi
  exit 1
fi
echo "[INFO] Python 依赖自检通过 ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

# ── 校验代码版本 ───────────────────────────────────────────────────────────────
if [[ ! -f "$PKG_DIR/batch_sync.py" ]]; then
  echo "ERROR: $PKG_DIR/batch_sync.py 不存在。" >&2
  echo "  请在服务器上部署代码（勿解压到 Jenkins workspace），例如：" >&2
  echo "    cd $SCRIPT_DIR && get2 job_info_sync_datahub.tar.gz && tar xzf job_info_sync_datahub.tar.gz" >&2
  echo "  或 export JOB_INFO_SYNC_DIR=/绝对路径/job_info_sync_datahub（该目录下应有 batch_sync.py）" >&2
  exit 1
fi
if ! grep -q 'audit-jsonl' "$PKG_DIR/batch_sync.py"; then
  echo "ERROR: batch_sync.py 版本过旧（缺少 --audit-jsonl），请重新打包上传 FTP" >&2
  exit 1
fi
echo "[INFO] batch_sync.py 版本验证通过"

# ── 每次运行前清理缓存（RETRY_FAILED=1 时跳过，用于断点续跑）─────────────────────────
_REPORT="$REPORT_DIR/batch_report.jsonl"
if [[ "$RETRY_FAILED" != "1" ]]; then
  if [[ -f "$_REPORT" ]]; then
    echo "[INFO] 清空旧报告文件（全量重跑）: $_REPORT"
    : > "$_REPORT"
  fi
  # 清理 __pycache__ 防止 Python 加载旧字节码
  find "$PKG_DIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  echo "[INFO] __pycache__ 已清理: $PKG_DIR"
fi

# ── 构建参数 ───────────────────────────────────────────────────────────────────
ARGS="--report $_REPORT"
ARGS="$ARGS --audit-jsonl $REPORT_DIR/lineage_audit.jsonl"
ARGS="$ARGS --concurrency $CONCURRENCY"
ARGS="$ARGS --llm-timeout $LLM_TIMEOUT"
[[ "$DRY_RUN"      == "1" ]] && ARGS="$ARGS --dry-run"
[[ "$LINEAGE_VOTE" == "1" ]] && ARGS="$ARGS --lineage-vote"

if [[ -n "$JOB_FILE" ]]; then
  ARGS="$ARGS --job-file $JOB_FILE"
elif [[ "$RETRY_FAILED" == "1" ]]; then
  ARGS="$ARGS --retry-failed $REPORT_DIR/batch_report.jsonl"
else
  [[ -z "$PREFIX" ]] && { echo "ERROR: 请设置 PREFIX 环境变量" >&2; exit 1; }
  ARGS="$ARGS --prefix $PREFIX"
fi

# ── 运行 batch_sync ───────────────────────────────────────────────────────────
echo "[INFO] running: $PYTHON -m job_info_sync_datahub.batch_sync $ARGS"
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.batch_sync $ARGS

# ── 导出 Excel ────────────────────────────────────────────────────────────────
echo "[INFO] exporting Excel report..."
PYTHONPATH="$PYTHONPATH_ROOT" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON" -m job_info_sync_datahub.export_lineage_excel \
    --audit       "$REPORT_DIR/lineage_audit.jsonl" \
    --report      "$REPORT_DIR/batch_report.jsonl" \
    --output      "$REPORT_DIR/lineage_report.xlsx"

echo "[DONE] reports -> $REPORT_DIR/"
