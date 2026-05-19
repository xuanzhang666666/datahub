#!/usr/bin/env python3
"""将 batch 报告 + 可选审计 JSONL 合并导出为 Excel。

列：作业名、信任度分数、最终目标表list、最终来源表list、
    deepseek目标表list、deepseek来源表list、
    sqlglot目标表list、sqlglot来源表list、
    解析状态、异常原因大分类、异常原因明细

依赖：pip install openpyxl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"


def _semi(names: Optional[List[str]]) -> str:
    if not names:
        return ""
    return ";".join(sorted(set(str(x).strip() for x in names if x and str(x).strip())))


def _load_audit_by_job(path: Path) -> Dict[str, Dict[str, Any]]:
    """加载 lineage_audit.jsonl，以 job 为 key（最后一条覆盖前面）。"""
    out: Dict[str, Dict[str, Any]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        job = rec.get("job")
        if isinstance(job, str) and job:
            out[job] = rec
    return out


def _load_report_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _big_category(report: Dict[str, Any], audit: Dict[str, Any]) -> str:
    st = report.get("status") or ""
    fc = report.get("fail_category")
    ls = audit.get("lineage_status") or report.get("lineage_status") or ""

    if st == "SKIP" or ls == "SKIP_NO_SQL":
        return "跳过"
    if st == "FAIL":
        return str(fc or "失败")
    if ls == "NORMAL_AGREE":
        return "正常-完全一致"
    if ls == "DISAGREE_DEEPSEEK_WINS":
        return "异常-分歧(DeepSeek为准)"
    if ls == "ABNORMAL_SINGLE_DEEPSEEK":
        return "异常-仅DeepSeek有结果"
    if ls == "ABNORMAL_SINGLE_SQLGLOT":
        return "异常-仅sqlglot有结果"
    if ls == "SKIP_NO_TABLES":
        return "跳过-无目标表"
    if ls == "LLM_POLICY_ERROR":
        return "异常-LLM调用失败"
    if fc:
        return str(fc)
    return st or ls or ""


def _detail(report: Dict[str, Any], audit: Dict[str, Any]) -> str:
    for key in ("lineage_reason", "error"):
        v = audit.get(key) if audit else None
        if v is None:
            v = report.get(key)
        if v:
            return str(v)[:200]
    return ""


def _status_text(report: Dict[str, Any], audit: Dict[str, Any]) -> str:
    parts: List[str] = []
    if report.get("status"):
        parts.append(str(report["status"]))
    ls = audit.get("lineage_status") or report.get("lineage_status")
    if ls:
        parts.append(str(ls))
    return " / ".join(parts) if parts else ""


def build_rows(
    report_path: Path,
    audit_path: Optional[Path],
) -> List[List[str]]:
    audit_map = _load_audit_by_job(audit_path) if audit_path else {}
    reports = _load_report_rows(report_path)
    rows: List[List[str]] = []

    for rep in reports:
        job = str(rep.get("job") or "")
        if not job:
            continue
        aud = audit_map.get(job, {})

        # 信任度
        trust_score = str(aud.get("trust_score", ""))

        # 最终选中的目标/来源表
        final_t = aud.get("targets_chosen") or rep.get("lineage_targets_chosen") or []
        final_u = aud.get("sources_chosen") or rep.get("lineage_sources_chosen") or []
        if not isinstance(final_t, list):
            final_t = []
        if not isinstance(final_u, list):
            final_u = []

        # DeepSeek 列
        ds_err = aud.get("deepseek_error") or rep.get("deepseek_error") or ""
        ds_t_raw = aud.get("targets_deepseek")
        ds_u_raw = aud.get("sources_deepseek")
        if not aud:
            ds_t_str = ds_u_str = "未调用LLM"
        elif ds_err and not ds_t_raw and not ds_u_raw:
            ds_t_str = ds_u_str = f"LLM失败:{ds_err[:100]}"
        else:
            ds_t_str = _semi(ds_t_raw if isinstance(ds_t_raw, list) else [])
            ds_u_str = _semi(ds_u_raw if isinstance(ds_u_raw, list) else [])

        # sqlglot 列
        sg_t_raw = aud.get("targets_sqlglot") or rep.get("sqlglot_targets") or []
        sg_u_raw = aud.get("sources_sqlglot") or rep.get("sqlglot_upstream") or []
        sg_t_str = _semi(sg_t_raw if isinstance(sg_t_raw, list) else [])
        sg_u_str = _semi(sg_u_raw if isinstance(sg_u_raw, list) else [])

        rows.append(
            [
                job,
                trust_score,
                _semi(final_t),
                _semi(final_u),
                ds_t_str,
                ds_u_str,
                sg_t_str,
                sg_u_str,
                _status_text(rep, aud),
                _big_category(rep, aud),
                _detail(rep, aud),
                str(rep.get("llm_raw_export_path") or ""),
                str(rep.get("etl_file_export_path") or ""),
            ]
        )
    return rows


HEADERS = [
    "作业名",
    "信任度分数",
    "最终目标表list",
    "最终来源表list",
    "deepseek目标表list",
    "deepseek来源表list",
    "sqlglot目标表list",
    "sqlglot来源表list",
    "解析状态",
    "异常原因大分类",
    "异常原因明细",
    "DeepSeek原始响应路径",
    "ETL脚本快照路径",
]


def write_xlsx(path: Path, rows: List[List[str]]) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise SystemExit("请先安装: pip install openpyxl") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "lineage"

    ws.append(HEADERS)
    hdr_row = ws[1]
    for cell in hdr_row:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9E1F2")

    for r in rows:
        ws.append(r)

    # 自适应列宽（最大 80）
    for col_idx, _ in enumerate(HEADERS, start=1):
        col_letter = get_column_letter(col_idx)
        max_len = len(HEADERS[col_idx - 1])
        for cell in ws[col_letter]:
            if cell.value:
                max_len = min(max(max_len, len(str(cell.value))), 80)
        ws.column_dimensions[col_letter].width = max_len + 2

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", required=True, type=Path, help="batch_report.jsonl 路径")
    p.add_argument("--audit", type=Path, default=None, help="lineage_audit.jsonl 路径（含 sqlglot/deepseek 结果）")
    p.add_argument("-o", "--output", type=Path, required=True, help="输出 .xlsx 路径")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    rows = build_rows(args.report, args.audit)
    write_xlsx(args.output, rows)
    print(f"已写入 {len(rows)} 行 -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
