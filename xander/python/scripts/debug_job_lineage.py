#!/usr/bin/env python3
"""本地排查调度作业血缘 — 打印或导出 DMP / ETL / LLM 各阶段中间结果。

在 ``xander/python`` 目录下运行::

  export BLF_LINEAGE_ENV_FILE=/path/to/lineage.env
  PYTHONPATH=. python3 scripts/debug_job_lineage.py --job <job_display_name>

常用::

  # 人类可读分阶段输出（默认）
  python3 scripts/debug_job_lineage.py -j dim_takeaway_region_manager_info

  # 仅 JSON（便于 jq / 脚本处理）
  python3 scripts/debug_job_lineage.py -j my_job --format json

  # 一行摘要
  python3 scripts/debug_job_lineage.py -j my_job --format summary

  # 多个作业 / 作业列表文件
  python3 scripts/debug_job_lineage.py -j job_a -j job_b
  python3 scripts/debug_job_lineage.py --job-file /path/jobs.txt

  # 不调用 LLM；或复用已保存的 llm_extract.json
  python3 scripts/debug_job_lineage.py -j my_job --skip-llm
  python3 scripts/debug_job_lineage.py -j my_job --llm-raw /tmp/llm_extract.json

  # 默认写入 /tmp/job_lineage_debug/<job>/debug_report.json
  python3 scripts/debug_job_lineage.py -j my_job -o /tmp/job_lineage_debug

也可在代码中调用::

  from job_info_sync_datahub.debug_lineage import DebugLineageConfig, debug_job_lineage
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PKG_ROOT = _SCRIPT_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from job_info_sync_datahub.debug_lineage import (  # noqa: E402
    DebugLineageConfig,
    debug_jobs,
    load_lineage_env_files,
    resolve_job_names,
)
from job_info_sync_datahub.logging_utils import setup_logging  # noqa: E402


def _default_output_dir() -> str:
    return os.environ.get("BLF_LINEAGE_DEBUG_OUTPUT_DIR", "/tmp/job_lineage_debug")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="调试单/多作业血缘解析（不写入 DataHub）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-j", "--job", action="append", default=[], help="job_display_name，可重复")
    p.add_argument("--job-file", help="每行一个 job_display_name（# 开头为注释）")
    p.add_argument("--env-file", action="append", default=[], help="额外 .env，可多次")
    p.add_argument(
        "--format",
        choices=("human", "json", "summary"),
        default="human",
        help="输出格式，默认 human",
    )
    p.add_argument("-o", "--output-dir", default=None, help=f"写入 debug_report.json，默认 {_default_output_dir()}")
    p.add_argument("--no-write-files", action="store_true", help="不写入 output-dir 文件")
    p.add_argument("--etl-file", help="本地 ETL 文件，跳过 GitLab")
    p.add_argument("--gitlab-file-path", help="指定 GitLab 仓库内路径")
    p.add_argument("--gitlab-token", default=os.getenv("BLF_GITLAB_PRIVATE_TOKEN"))
    p.add_argument("--gitlab-ref", default="master")
    p.add_argument("--skip-llm", action="store_true")
    p.add_argument("--skip-hive-check", action="store_true")
    p.add_argument("--llm-timeout", type=int, default=120)
    p.add_argument("--llm-raw", dest="llm_raw_file", help="复用已有 LLM JSON，不再请求 API")
    p.add_argument("--etl-preview-chars", type=int, default=4000, help="ETL 预览字符数，0=不预览")
    p.add_argument("--platform-instance", default=os.getenv("BLF_PLATFORM_INSTANCE", "blf-prod-hive"))
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"), help="DataHub env，用于 URN 预览")
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(level=args.log_level)
    loaded = load_lineage_env_files(args.env_file or None)
    if loaded and args.log_level in ("DEBUG", "INFO"):
        print(f"[env] loaded: {', '.join(loaded)}", file=sys.stderr)

    try:
        jobs = resolve_job_names(args.job, args.job_file)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out_dir = None if args.no_write_files else (args.output_dir or _default_output_dir())
    cfg = DebugLineageConfig(
        etl_file=args.etl_file,
        gitlab_token=args.gitlab_token,
        gitlab_ref=args.gitlab_ref,
        gitlab_file_path=args.gitlab_file_path,
        llm_timeout_sec=args.llm_timeout,
        skip_llm=args.skip_llm,
        skip_hive_check=args.skip_hive_check,
        llm_raw_file=args.llm_raw_file,
        platform_instance=args.platform_instance,
        env=args.env,
        etl_preview_chars=args.etl_preview_chars,
        output_dir=out_dir,
        output_format=args.format,
    )

    try:
        code, _ = debug_jobs(jobs, cfg)
        return code
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
