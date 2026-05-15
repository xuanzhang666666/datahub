#!/usr/bin/env python3
"""从调度导出的 Excel（job 名 + shell_command）批量套用血缘里的作业文件名逻辑，输出分析表。

与表级血缘一致：``has_real_w_run_task`` → ``extract_job_path_and_type`` → ``job_file_name``，
并附 ``get_job_dir_name``、``candidate_gitlab_paths`` 首条候选路径便于对照仓库。

示例::

  cd xander/python
  PYTHONPATH=. python3 scripts/jenkins_shell_to_jobfile_report.py \\
    --input ~/Downloads/jenkins_command.xlsx \\
    --output ~/Downloads/jenkins_command_job_files.xlsx

默认跳过第 1 行表头；若第 1 行也是数据，加 ``--no-skip-header``。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PY_ROOT = _SCRIPT_DIR.parent
if str(_PY_ROOT) not in sys.path:
    sys.path.insert(0, str(_PY_ROOT))

from openpyxl import Workbook, load_workbook  # noqa: E402

from job_info_sync_datahub.runtime_parser import (  # noqa: E402
    candidate_gitlab_paths,
    extract_gitlab_name,
    extract_job_path_and_type,
    get_job_dir_name,
    has_real_w_run_task,
    job_file_name,
)


def _cell_str(val: object) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    return str(val).strip()


def _resolve_row(shell_raw: str) -> tuple[str, str, str, str, str, str, str]:
    """返回 (gitlab_name, job_path, kind, job_file_name, first_candidate, status, detail)。"""
    shell = shell_raw.strip() if shell_raw else ""
    if not shell:
        return "", "", "", "", "", "empty_shell", ""

    if not has_real_w_run_task(shell):
        return "", "", "", "", "", "no_runner", "非注释行中未匹配 /bin/*-run-task.sh"

    gitlab_name = ""
    try:
        gitlab_name = extract_gitlab_name(shell)
    except RuntimeError as e:
        gitlab_name = ""
        # 仍尝试路径解析（部分 shell 可能能解析 job_path）
    try:
        job_path, kind = extract_job_path_and_type(shell)
    except RuntimeError as e:
        return gitlab_name, "", "", "", "", "parse_job_path", str(e)[:500]

    jfn = job_file_name(job_path, kind)
    job_dir = get_job_dir_name(shell)
    cands = candidate_gitlab_paths(job_path, jfn, job_dir)
    first = cands[0] if cands else ""
    return gitlab_name, job_path, kind, jfn, first, "ok", ""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--input",
        "-i",
        default=os.path.expanduser("~/Downloads/jenkins_command.xlsx"),
        help="输入 xlsx（第 1 列 job 名，第 2 列 shell_command）",
    )
    p.add_argument(
        "--output",
        "-o",
        default="",
        help="输出 xlsx；默认与输入同目录 jenkins_command_job_files.xlsx",
    )
    p.add_argument(
        "--sheet",
        default="",
        help="读取的工作表名；默认用活动表",
    )
    p.add_argument(
        "--no-skip-header",
        action="store_true",
        help="第 1 行即数据（不跳过表头行）",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    inp = Path(os.path.expanduser(args.input)).resolve()
    if not inp.is_file():
        print(f"ERROR: 输入文件不存在: {inp}", file=sys.stderr)
        return 2

    out = args.output.strip()
    if not out:
        out = str(inp.parent / "jenkins_command_job_files.xlsx")
    outp = Path(os.path.expanduser(out)).resolve()

    skip_header = not args.no_skip_header

    wb_in = load_workbook(inp, read_only=True, data_only=True)
    ws_name = args.sheet.strip() or wb_in.sheetnames[0]
    ws_in = wb_in[ws_name]

    wb_out = Workbook()
    ws_out = wb_out.active
    ws_out.title = "job_files"
    headers = [
        "job_name",
        "status",
        "detail",
        "gitlab_name",
        "job_path",
        "kind",
        "job_file_name",
        "first_gitlab_candidate",
        "shell_preview",
    ]
    ws_out.append(headers)

    min_row = 2 if skip_header else 1
    row_count = 0
    status_counts: dict[str, int] = {}
    for row in ws_in.iter_rows(min_row=min_row, max_col=2, values_only=True):
        job_name = _cell_str(row[0]) if row else ""
        shell_raw = row[1] if len(row) > 1 else ""
        shell = _cell_str(shell_raw) if shell_raw is not None else ""
        if not job_name and not shell:
            continue

        gname, jpath, kind, jfn, cand, status, detail = _resolve_row(shell)
        preview = shell[:800] if shell else ""
        ws_out.append([job_name, status, detail, gname, jpath, kind, jfn, cand, preview])
        row_count += 1
        status_counts[status] = status_counts.get(status, 0) + 1

    wb_in.close()
    wb_out.save(outp)
    print(f"Wrote {row_count} rows -> {outp}")
    for k in sorted(status_counts.keys()):
        print(f"  {k}: {status_counts[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
