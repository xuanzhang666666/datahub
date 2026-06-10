#!/usr/bin/env python3
"""CLI for the independent field-level lineage review workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

from .field_lineage_datahub_reader import (
    extract_field_lineage_input_with_debug,
    extract_target_partition_fields_from_script,
    fetch_deprecation,
    fetch_schema_fields_with_partitions,
    fetch_structured_properties,
    fetch_upstream_table_names,
    has_confirmed_field_lineage,
    is_deprecated_dataset_payload,
    make_hive_dataset_urn,
    missing_field_lineage_source_reason,
    validate_source_tables,
    write_field_lineage_debug_artifacts,
)
from .field_lineage_batch_summary import summarize_review_workbook

# 无 Etl Script / structured property 内容时跳过 LLM（shell 脚本据此汇总）
EXIT_SKIP_NO_SOURCE = 3
# --write 时 Excel 无 APPROVED 行
EXIT_NO_APPROVED_ROWS = 4
# --write 时目标表已被 Data Availability Flag 标记为字段血缘已确认
EXIT_CONFIRMED_FIELD_LINEAGE = 5
# 自动导入模式下，Excel 未达到 100% AUTO_APPROVED 且 unresolved_field_count=0
EXIT_REQUIRES_REVIEW = 6
# --write 时导入行未通过 source_table 二次校验
EXIT_IMPORT_VALIDATION_FAILED = 7
from .field_lineage_excel import (
    fold_ephemeral_source_candidates,
    load_approved_review_rows,
    validate_import_candidates,
    write_candidate_workbook,
)
from .field_lineage_llm import call_llm_extract_field_lineage
from .field_lineage_models import FieldLineageCandidate, FieldLineageReviewStatus
from .field_lineage_policy import normalize_source_table_name
from .field_lineage_writer import write_approved_field_lineages


def _log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[FIELD_LINEAGE][{now}] {message}", flush=True)


def _log_import_summary(summary: dict[str, object], output: Optional[Path]) -> None:
    _log(
        "import-reviewed summary: "
        f"approved_rows={summary['approved_rows']} "
        f"dry_run={summary['dry_run']} "
        f"auto_approved_percent={float(summary['auto_approved_percent']):.2%} "
        f"unresolved_field_count={summary['unresolved_field_count']}"
    )
    if output:
        _log(f"import_plan={output}")
    write_result = summary.get("write_result")
    if not isinstance(write_result, dict):
        return
    tables = write_result.get("tables")
    if not isinstance(tables, dict):
        return
    for table_name, raw_table_result in sorted(tables.items()):
        if not isinstance(raw_table_result, dict):
            continue
        message = (
            f"table={table_name} "
            f"field_count={raw_table_result.get('field_count', 0)} "
            f"written={raw_table_result.get('written', False)}"
        )
        completeness = raw_table_result.get("field_lineage_completeness")
        if isinstance(completeness, dict):
            missing_fields = completeness.get("missing_fields")
            missing_count = len(missing_fields) if isinstance(missing_fields, list) else 0
            message += (
                f" completeness={completeness.get('is_complete', False)} "
                f"covered={completeness.get('covered_field_count', 0)}/"
                f"{completeness.get('schema_field_count', 0)} "
                f"missing={missing_count} "
                f"marked_flag={completeness.get('marked_data_availability_flag', False)}"
            )
        _log(message)


def _cmd_export(args: argparse.Namespace) -> int:
    if args.llm_provider:
        os.environ["LLM_PROVIDER"] = args.llm_provider
    if args.llm_model:
        os.environ["LLM_MODEL"] = args.llm_model
    dataset_urn = make_hive_dataset_urn(args.table, args.platform_instance, args.env)
    _log("export started")
    _log(f"table={args.table}")
    _log(f"dataset_urn={dataset_urn}")
    _log(f"gms_url={args.gms_url}")
    _log(f"platform_instance={args.platform_instance} env={args.env}")
    _log(f"output={args.output}")
    _log(f"llm_timeout_sec={args.llm_timeout_sec}")
    _log("reading DataHub deprecation ...")
    if is_deprecated_dataset_payload(
        fetch_deprecation(args.gms_url, dataset_urn, token=args.gms_token)
    ):
        skip_reason = "Dataset 已标记废弃，跳过字段血缘导出"
        _log(f"SKIP: {skip_reason}")
        print(f"FIELD_LINEAGE_SKIP_REASON={skip_reason}", flush=True)
        return EXIT_SKIP_NO_SOURCE
    _log("reading DataHub structuredProperties ...")
    payload = fetch_structured_properties(
        args.gms_url,
        dataset_urn,
        token=args.gms_token,
    )
    if has_confirmed_field_lineage(payload) and not args.force_refresh_field_lineage:
        skip_reason = "Data Availability Flag 已包含「字段血缘」，字段血缘已确认，跳过导出"
        _log(f"SKIP: {skip_reason}")
        print(f"FIELD_LINEAGE_SKIP_REASON={skip_reason}", flush=True)
        return EXIT_SKIP_NO_SOURCE
    if has_confirmed_field_lineage(payload) and args.force_refresh_field_lineage:
        _log(
            "WARN: Data Availability Flag 已包含「字段血缘」，但 --force-refresh-field-lineage 已开启，继续重导出"
        )
    skip_reason = missing_field_lineage_source_reason(payload)
    if skip_reason:
        _log(f"SKIP: {skip_reason}")
        print(f"FIELD_LINEAGE_SKIP_REASON={skip_reason}", flush=True)
        return EXIT_SKIP_NO_SOURCE
    source_input, preparation_debug = extract_field_lineage_input_with_debug(
        dataset_urn,
        args.table,
        payload,
    )
    _log("reading DataHub schemaMetadata ...")
    try:
        target_schema_fields, target_partition_fields = fetch_schema_fields_with_partitions(
            args.gms_url,
            dataset_urn,
            token=args.gms_token,
        )
    except RuntimeError as exc:
        target_schema_fields = []
        target_partition_fields = []
        _log(f"warning: schemaMetadata read failed, continue without DDL order: {exc}")
    script_partition_fields = extract_target_partition_fields_from_script(
        source_input.etl_script,
        source_input.table_name,
        aliases=source_input.target_table_aliases,
    )
    target_partition_fields = list(
        dict.fromkeys([*target_partition_fields, *script_partition_fields])
    )
    upstream_tables = sorted(
        fetch_upstream_table_names(
            args.gms_url,
            dataset_urn,
            token=args.gms_token,
            platform_instance=args.platform_instance,
        )
    )
    _log(f"target_direct_upstream_table_count={len(upstream_tables)}")
    source_input = replace(
        source_input,
        target_schema_fields=target_schema_fields,
        target_partition_fields=target_partition_fields,
        allowed_upstream_tables=upstream_tables,
    )
    debug_dir = args.debug_dir or args.output.with_suffix("").with_name(
        f"{args.output.stem}_debug"
    )
    written_debug_files = write_field_lineage_debug_artifacts(
        debug_dir,
        source_input,
        preparation_debug,
    )
    _log("structuredProperties loaded")
    _log(f"target_schema_field_count={len(source_input.target_schema_fields)}")
    _log(f"target_partition_fields={','.join(source_input.target_partition_fields)}")
    _log(f"etl_script_chars={len(source_input.etl_script)}")
    _log(f"execute_shell_chars={len(source_input.execute_shell)}")
    _log(f"debug artifacts dir={debug_dir}")
    _log(f"debug artifacts files={','.join(written_debug_files)}")
    if source_input.execute_shell:
        _log("execute_shell begin")
        print(source_input.execute_shell, flush=True)
        _log("execute_shell end")
    _log("etl_script saved to debug artifacts; console preview disabled")
    _log("calling LLM for field lineage candidates ...")
    parsed, model = call_llm_extract_field_lineage(
        source_input,
        timeout_sec=args.llm_timeout_sec,
    )
    _log("LLM returned")
    _log(f"llm_model={model}")
    folded_mappings = fold_ephemeral_source_candidates(
        parsed.mappings,
        set(upstream_tables),
    )
    folded_count = sum(
        1
        for before, after in zip(parsed.mappings, folded_mappings, strict=True)
        if before.source_table != after.source_table
    )
    ephemeral_blocked = sum(
        1 for candidate in folded_mappings if "EPHEMERAL_SOURCE_MUST_FOLD" in candidate.import_error
    )
    parsed = replace(parsed, mappings=folded_mappings)
    _log(f"candidate_count={len(parsed.mappings)}")
    _log(f"folded_source_table_count={folded_count}")
    _log(f"ephemeral_source_blocked_count={ephemeral_blocked}")
    _log(f"unresolved_field_count={len(parsed.unresolved_fields)}")
    source_tables = {
        normalize_source_table_name(candidate.source_table)
        for candidate in parsed.mappings
        if candidate.source_table.strip()
    }
    _log(
        "validating candidate source tables against DataHub datasets and target upstreamLineage ..."
    )
    source_table_validations = validate_source_tables(
        args.gms_url,
        dataset_urn,
        source_tables,
        token=args.gms_token,
        platform_instance=args.platform_instance,
        env=args.env,
    )
    invalid_source_tables = [
        validation
        for validation in source_table_validations.values()
        if not validation.dataset_exists or not validation.in_target_upstreams
    ]
    _log(
        f"source_table_validation_count={len(source_table_validations)} "
        f"invalid_count={len(invalid_source_tables)}"
    )
    for validation in invalid_source_tables:
        _log(
            "source table validation failed: "
            f"source={validation.source_table} "
            f"dataset_exists={validation.dataset_exists} "
            f"in_target_upstreams={validation.in_target_upstreams}"
        )
    _log("writing review Excel ...")
    write_candidate_workbook(
        args.output,
        source_input=source_input,
        candidates=parsed.mappings,
        unresolved_fields=parsed.unresolved_fields,
        llm_model=model,
        debug_dir=debug_dir,
        source_table_validations=source_table_validations,
    )
    _log(f"export finished: {len(parsed.mappings)} candidates -> {args.output}")
    return 0


def _cmd_import_reviewed(args: argparse.Namespace) -> int:
    import_statuses = _parse_import_statuses(args.import_statuses)
    workbook_summary = summarize_review_workbook(args.input)
    if args.require_full_auto_approved and not workbook_summary.is_fully_auto_approved:
        _log(
            "SKIP_REQUIRES_REVIEW: "
            f"auto_approved_percent={workbook_summary.auto_approved_percent:.2%} "
            f"unresolved_field_count={workbook_summary.unresolved_field_count}"
        )
        print(
            "FIELD_LINEAGE_SKIP_REASON="
            "REQUIRES_REVIEW:"
            f"auto_approved_percent={workbook_summary.auto_approved_percent:.2%},"
            f"unresolved_field_count={workbook_summary.unresolved_field_count}",
            flush=True,
        )
        return EXIT_REQUIRES_REVIEW
    approved = load_approved_review_rows(args.input, import_statuses=import_statuses)
    status_values = sorted(status.value for status in import_statuses)
    _log(
        "import-reviewed started: "
        f"approved_rows={len(approved)} write={args.write} import_statuses={','.join(status_values)}"
    )
    if not approved:
        _log("no APPROVED rows, nothing to import")
        if args.write:
            _log(
                "ERROR: 已指定 --write 但 Excel 的 candidate_lineage 中无 review_status=APPROVED 行；"
                "请人工审核后将需导入行的 review_status 改为 APPROVED 后重试"
            )
            return EXIT_NO_APPROVED_ROWS
        return 0

    missing_expr = [
        f"{r.target_table}.{r.target_field}"
        for r in approved
        if not r.transform_expression.strip()
    ]
    missing_explanation = [
        f"{r.target_table}.{r.target_field}"
        for r in approved
        if not r.transform_explanation.strip()
    ]
    if missing_expr:
        _log(
            "warning: 以下字段未填写 transform_expression，UI 侧栏将无 SQL 表达式: "
            + ", ".join(missing_expr[:20])
        )
    if missing_explanation:
        _log(
            "warning: 以下字段未填写 transform_explanation，UI 侧栏将无中文解释: "
            + ", ".join(missing_explanation[:20])
        )

    if args.write:
        validated_rows: list[FieldLineageCandidate] = []
        validation_errors: list[str] = []
        for target_table in sorted({row.target_table for row in approved if row.target_table}):
            rows = [row for row in approved if row.target_table == target_table]
            source_tables = {
                normalize_source_table_name(row.source_table)
                for row in rows
                if row.source_table.strip()
            }
            dataset_urn = make_hive_dataset_urn(
                target_table,
                args.platform_instance,
                args.env,
            )
            source_table_validations = (
                validate_source_tables(
                    args.gms_url,
                    dataset_urn,
                    source_tables,
                    token=args.gms_token,
                    platform_instance=args.platform_instance,
                    env=args.env,
                )
                if source_tables
                else {}
            )
            accepted, errors = validate_import_candidates(rows, source_table_validations)
            validated_rows.extend(accepted)
            validation_errors.extend(errors)
        if validation_errors:
            _log(
                "ERROR: import-reviewed 二次校验失败，已阻止写入 DataHub；"
                f"invalid_rows={len(validation_errors)}"
            )
            for error in validation_errors[:50]:
                _log(error)
            if len(validation_errors) > 50:
                _log(f"... 另有 {len(validation_errors) - 50} 条校验错误未展示")
            return EXIT_IMPORT_VALIDATION_FAILED
        approved = validated_rows
        if not approved:
            _log("ERROR: 二次校验后无可导入行")
            return EXIT_IMPORT_VALIDATION_FAILED

    if args.write:
        protected_tables = []
        target_tables = sorted({row.target_table for row in approved if row.target_table})
        for target_table in target_tables:
            dataset_urn = make_hive_dataset_urn(
                target_table,
                args.platform_instance,
                args.env,
            )
            payload = fetch_structured_properties(
                args.gms_url,
                dataset_urn,
                token=args.gms_token,
            )
            if has_confirmed_field_lineage(payload) and not args.force_refresh_field_lineage:
                protected_tables.append(target_table)
        if protected_tables:
            _log(
                "ERROR: 以下表 Data Availability Flag 已包含「字段血缘」，视为已确认，禁止修改: "
                + ", ".join(protected_tables)
            )
            return EXIT_CONFIRMED_FIELD_LINEAGE

    result = write_approved_field_lineages(
        args.gms_url,
        approved,
        token=args.gms_token,
        platform_instance=args.platform_instance,
        env=args.env,
        dry_run=not args.write,
        clear_existing=args.clear_existing,
    )
    summary = {
        "input": str(args.input),
        "approved_rows": len(approved),
        "import_statuses": status_values,
        "auto_approved_percent": workbook_summary.auto_approved_percent,
        "unresolved_field_count": workbook_summary.unresolved_field_count,
        "require_full_auto_approved": args.require_full_auto_approved,
        "dry_run": not args.write,
        "clear_existing": args.clear_existing,
        "rows": [
            {
                "target_table": row.target_table,
                "target_field": row.target_field,
                "source_table": row.source_table,
                "source_field": row.source_field,
                "transform_expression": row.transform_expression,
                "transform_explanation": row.transform_explanation,
                "confidence": row.confidence,
            }
            for row in approved
        ],
        "write_result": result,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _log_import_summary(summary, args.output)
    if args.write:
        _log("import-reviewed write finished")
    else:
        _log("import-reviewed dry-run finished (add --write to persist)")
    return 0


def _parse_import_statuses(raw: str) -> set[FieldLineageReviewStatus]:
    statuses: set[FieldLineageReviewStatus] = set()
    for item in raw.split(","):
        value = item.strip().upper()
        if not value:
            continue
        try:
            statuses.add(FieldLineageReviewStatus(value))
        except ValueError as exc:
            allowed = ", ".join(status.value for status in FieldLineageReviewStatus)
            raise RuntimeError(f"无效 import status: {value}；允许值: {allowed}") from exc
    if not statuses:
        raise RuntimeError("import statuses 为空")
    return statuses


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="读取 DataHub structuredProperties 并导出候选 Excel")
    export.add_argument("--table", required=True, help="目标表，例如 default.dim_store_info")
    export.add_argument("--output", required=True, type=Path, help="输出 .xlsx 路径")
    export.add_argument(
        "--debug-dir",
        type=Path,
        default=None,
        help="保存变量替换、裁剪后脚本等中间产物；默认写到 Excel 同目录的 *_debug",
    )
    export.add_argument(
        "--gms-url",
        default=os.getenv("DATAHUB_GMS_URL", "http://datahub-gms:8080"),
        help="DataHub GMS URL",
    )
    export.add_argument("--gms-token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    export.add_argument("--platform-instance", default="blf-prod-hive")
    export.add_argument("--env", default="PROD")
    export.add_argument("--llm-timeout-sec", type=int, default=180)
    export.add_argument("--llm-provider", default=os.getenv("LLM_PROVIDER", ""))
    export.add_argument("--llm-model", default=os.getenv("LLM_MODEL", ""))
    export.add_argument(
        "--preview-chars",
        type=int,
        default=int(os.getenv("FIELD_LINEAGE_PREVIEW_CHARS", "4000")),
        help="兼容旧 Jenkins 参数；ETL 脚本正文不再打印到 console，只保存到 debug artifacts",
    )
    export.add_argument(
        "--force-refresh-field-lineage",
        action="store_true",
        default=os.getenv("FIELD_LINEAGE_FORCE_REFRESH", "").lower() in {"1", "true", "yes"},
        help="忽略 Data Availability Flag「字段血缘」保护，强制重导出（用于修复脏数据）",
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
    import_reviewed.add_argument(
        "--clear-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="导入前清空目标表已有 fineGrainedLineages；默认开启。关闭时合并已有字段血缘",
    )
    import_reviewed.add_argument(
        "--import-statuses",
        default=os.getenv("FIELD_LINEAGE_IMPORT_STATUSES", "AUTO_APPROVED,APPROVED"),
        help="逗号分隔的可导入 review_status，默认 AUTO_APPROVED,APPROVED",
    )
    import_reviewed.add_argument(
        "--require-full-auto-approved",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("FIELD_LINEAGE_REQUIRE_FULL_AUTO_APPROVED", "0").lower()
        in {"1", "true", "yes"},
        help="要求 auto_approved_percent=100%% 且 unresolved_field_count=0，否则跳过等待人工审核",
    )
    import_reviewed.add_argument(
        "--force-refresh-field-lineage",
        action="store_true",
        default=os.getenv("FIELD_LINEAGE_FORCE_REFRESH", "").lower() in {"1", "true", "yes"},
        help="忽略 Data Availability Flag「字段血缘」保护，允许覆盖已有字段血缘",
    )
    import_reviewed.set_defaults(func=_cmd_import_reviewed)

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
