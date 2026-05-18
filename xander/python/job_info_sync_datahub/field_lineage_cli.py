#!/usr/bin/env python3
"""CLI for the independent field-level lineage review workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

from .field_lineage_datahub_reader import make_hive_dataset_urn, read_field_lineage_input
from .field_lineage_excel import load_approved_review_rows, write_candidate_workbook
from .field_lineage_llm import call_llm_extract_field_lineage
from .field_lineage_writer import write_approved_field_lineages


def _log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[FIELD_LINEAGE][{now}] {message}", flush=True)


def _preview(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated, total_chars={len(text)}]"


def _cmd_export(args: argparse.Namespace) -> int:
    dataset_urn = make_hive_dataset_urn(args.table, args.platform_instance, args.env)
    _log("export started")
    _log(f"table={args.table}")
    _log(f"dataset_urn={dataset_urn}")
    _log(f"gms_url={args.gms_url}")
    _log(f"platform_instance={args.platform_instance} env={args.env}")
    _log(f"output={args.output}")
    _log(f"llm_timeout_sec={args.llm_timeout_sec}")
    _log("reading DataHub structuredProperties ...")
    source_input = read_field_lineage_input(
        args.gms_url,
        args.table,
        token=args.gms_token,
        platform_instance=args.platform_instance,
        env=args.env,
    )
    _log("structuredProperties loaded")
    _log(f"etl_script_chars={len(source_input.etl_script)}")
    _log(f"execute_shell_chars={len(source_input.execute_shell)}")
    if source_input.execute_shell:
        _log("execute_shell begin")
        print(source_input.execute_shell, flush=True)
        _log("execute_shell end")
    _log(f"etl_script preview begin max_chars={args.preview_chars}")
    preview = _preview(source_input.etl_script, args.preview_chars)
    if preview:
        print(preview, flush=True)
    _log("etl_script preview end")
    _log("calling LLM for field lineage candidates ...")
    parsed, model = call_llm_extract_field_lineage(
        source_input,
        timeout_sec=args.llm_timeout_sec,
    )
    _log("LLM returned")
    _log(f"llm_model={model}")
    _log(f"candidate_count={len(parsed.mappings)}")
    _log(f"unresolved_field_count={len(parsed.unresolved_fields)}")
    _log("writing review Excel ...")
    write_candidate_workbook(
        args.output,
        source_input=source_input,
        candidates=parsed.mappings,
        unresolved_fields=parsed.unresolved_fields,
        llm_model=model,
    )
    _log(f"export finished: {len(parsed.mappings)} candidates -> {args.output}")
    return 0


def _cmd_import_reviewed(args: argparse.Namespace) -> int:
    approved = load_approved_review_rows(args.input)
    _log(f"import-reviewed started: approved_rows={len(approved)} write={args.write}")
    if not approved:
        _log("no APPROVED rows, nothing to import")
        return 0

    missing_expr = [
        f"{r.target_table}.{r.target_field}"
        for r in approved
        if not r.transform_expression.strip()
    ]
    if missing_expr:
        _log(
            "warning: 以下字段未填写 transform_expression，UI 侧栏将无 LOGIC 表达式: "
            + ", ".join(missing_expr[:20])
        )

    result = write_approved_field_lineages(
        args.gms_url,
        approved,
        token=args.gms_token,
        platform_instance=args.platform_instance,
        env=args.env,
        dry_run=not args.write,
    )
    summary = {
        "input": str(args.input),
        "approved_rows": len(approved),
        "dry_run": not args.write,
        "rows": [
            {
                "target_table": row.target_table,
                "target_field": row.target_field,
                "source_table": row.source_table,
                "source_field": row.source_field,
                "transform_expression": row.transform_expression,
                "confidence": row.confidence,
            }
            for row in approved
        ],
        "write_result": result,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.write:
        _log("import-reviewed write finished")
    else:
        _log("import-reviewed dry-run finished (add --write to persist)")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="读取 DataHub structuredProperties 并导出候选 Excel")
    export.add_argument("--table", required=True, help="目标表，例如 default.dim_store_info")
    export.add_argument("--output", required=True, type=Path, help="输出 .xlsx 路径")
    export.add_argument(
        "--gms-url",
        default=os.getenv("DATAHUB_GMS_URL", "http://datahub-gms:8080"),
        help="DataHub GMS URL",
    )
    export.add_argument("--gms-token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    export.add_argument("--platform-instance", default="blf-prod-hive")
    export.add_argument("--env", default="PROD")
    export.add_argument("--llm-timeout-sec", type=int, default=180)
    export.add_argument(
        "--preview-chars",
        type=int,
        default=int(os.getenv("FIELD_LINEAGE_PREVIEW_CHARS", "4000")),
        help="打印 ETL 脚本预览的最大字符数；0 表示不打印脚本内容",
    )
    export.set_defaults(func=_cmd_export)

    import_reviewed = sub.add_parser(
        "import-reviewed",
        help="读取人工审核 Excel，只生成 APPROVED 行的导入计划",
    )
    import_reviewed.add_argument("--input", required=True, type=Path)
    import_reviewed.add_argument("--output", type=Path, default=None)
    import_reviewed.add_argument(
        "--gms-url",
        default=os.getenv("DATAHUB_GMS_URL", "http://datahub-gms:8080"),
    )
    import_reviewed.add_argument("--gms-token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    import_reviewed.add_argument("--platform-instance", default="blf-prod-hive")
    import_reviewed.add_argument("--env", default="PROD")
    import_reviewed.add_argument(
        "--write",
        action="store_true",
        help="写入 DataHub fineGrainedLineages（含 transformOperation）；默认仅 dry-run",
    )
    import_reviewed.set_defaults(func=_cmd_import_reviewed)

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
