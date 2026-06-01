#!/usr/bin/env python3
"""Batch-update data_availability_flag for eligible Hive views.

Eligibility (same criteria as view export report):
  - upstream_count > 0
  - view_definition_has_content == 是  (viewProperties.viewLogic)

Target flags: DDL, 表血缘, 字段血缘 (via patch_data_availability_flags).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .check_dataset_availability import (
    FLAG_DDL,
    FLAG_FIELD_LINEAGE,
    FLAG_TABLE_LINEAGE,
    AvailabilityResult,
    extract_existing_flags,
    extract_structured_values,
    fetch_structured_properties_or_empty,
    patch_data_availability_flags,
    result_to_row,
    write_jsonl_report,
    write_xlsx_report,
)
from .export_view_datasets_report import ViewDatasetExportRow, build_view_export_rows
from .field_lineage_datahub_reader import make_hive_dataset_urn
from .logging_utils import get_logger, setup_logging

logger = get_logger("batch_update_view_availability_flags")

VIEW_FULL_AVAILABILITY_FLAGS = [FLAG_DDL, FLAG_TABLE_LINEAGE, FLAG_FIELD_LINEAGE]


@dataclass(frozen=True)
class EligibleViewRow:
    view_name: str
    upstream_count: int
    data_availability_flag_before: str
    view_definition_has_content: str


def is_eligible_view_export_row(row: ViewDatasetExportRow) -> bool:
    return row.upstream_count > 0 and row.view_definition_has_content == "是"


def filter_eligible_views(rows: list[ViewDatasetExportRow]) -> list[EligibleViewRow]:
    eligible: list[EligibleViewRow] = []
    for row in rows:
        if not is_eligible_view_export_row(row):
            continue
        eligible.append(
            EligibleViewRow(
                view_name=row.view_name,
                upstream_count=row.upstream_count,
                data_availability_flag_before=row.data_availability_flag,
                view_definition_has_content=row.view_definition_has_content,
            )
        )
    return eligible


def load_view_rows_from_csv(path: Path) -> list[ViewDatasetExportRow]:
    rows: list[ViewDatasetExportRow] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            view_name = (raw.get("view_name") or "").strip().lower()
            if not view_name:
                continue
            try:
                upstream_count = int(str(raw.get("upstream_count") or "0").strip())
            except ValueError:
                upstream_count = 0
            rows.append(
                ViewDatasetExportRow(
                    view_name=view_name,
                    upstream_count=upstream_count,
                    data_availability_flag=(raw.get("data_availability_flag") or "-").strip(),
                    view_definition_has_content=(
                        raw.get("view_definition_has_content") or "否"
                    ).strip(),
                )
            )
    return rows


def update_one_view_flag(
    table_name: str,
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
    dry_run: bool,
    target_flags: list[str],
) -> AvailabilityResult:
    normalized = table_name.strip().lower()
    dataset_urn = make_hive_dataset_urn(normalized, platform_instance, env)
    structured_payload = fetch_structured_properties_or_empty(gms_url, dataset_urn, token=token)
    existing_flags = extract_existing_flags(extract_structured_values(structured_payload))
    final_flags = list(target_flags)
    result = AvailabilityResult(
        table_name=normalized,
        dataset_urn=dataset_urn,
        dataset_type="view",
        passed_flags=set(final_flags),
        existing_flags=existing_flags,
        final_flags=final_flags,
        reason=(
            "批量设置 view Data Availability Flag = "
            f"{json.dumps(final_flags, ensure_ascii=False)}"
        ),
    )
    if set(final_flags) == existing_flags:
        result.write_status = "NO_CHANGE"
    elif dry_run:
        result.write_status = "DRY_RUN"
    else:
        patch_data_availability_flags(gms_url, dataset_urn, final_flags, token=token)
        result.write_status = "UPDATED"
    return result


def run_batch_update(
    eligible: list[EligibleViewRow],
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
    dry_run: bool,
    concurrency: int,
    jsonl_path: str,
    xlsx_path: str,
) -> int:
    rows: list[dict[str, Any]] = []
    failures = 0
    updated = 0
    no_change = 0
    dry_run_count = 0

    def _work(item: EligibleViewRow) -> AvailabilityResult:
        return update_one_view_flag(
            item.view_name,
            gms_url=gms_url,
            token=token,
            platform_instance=platform_instance,
            env=env,
            dry_run=dry_run,
            target_flags=VIEW_FULL_AVAILABILITY_FLAGS,
        )

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(_work, item): item for item in eligible}
        done = 0
        for future in as_completed(futures):
            done += 1
            item = futures[future]
            started = time.time()
            try:
                result = future.result()
                status = "OK"
            except Exception as exc:
                failures += 1
                status = "FAIL"
                result = AvailabilityResult(
                    table_name=item.view_name,
                    dataset_urn=make_hive_dataset_urn(
                        item.view_name, platform_instance, env
                    ),
                    dataset_type="view",
                    status="FAIL",
                    reason="PATCH data_availability_flag 失败",
                    error=str(exc)[:500],
                )
            if result.write_status == "UPDATED":
                updated += 1
            elif result.write_status == "NO_CHANGE":
                no_change += 1
            elif result.write_status == "DRY_RUN":
                dry_run_count += 1
            row = result_to_row(result)
            row["status"] = status
            row["upstream_count"] = item.upstream_count
            row["view_definition_has_content"] = item.view_definition_has_content
            row["data_availability_flag_before"] = item.data_availability_flag_before
            row["elapsed"] = round(time.time() - started, 2)
            rows.append(row)
            if done % 200 == 0 or done == len(eligible):
                logger.info(
                    "[PROGRESS] %s/%s updated=%s no_change=%s dry_run=%s fail=%s",
                    done,
                    len(eligible),
                    updated,
                    no_change,
                    dry_run_count,
                    failures,
                )

    rows.sort(key=lambda r: r.get("table_name", ""))
    write_jsonl_report(jsonl_path, rows)
    write_xlsx_report(xlsx_path, rows)
    print(
        f"[DONE] eligible={len(eligible)} updated={updated} no_change={no_change} "
        f"dry_run={dry_run_count} fail={failures}",
        flush=True,
    )
    print(f"[DONE] jsonl: {jsonl_path}", flush=True)
    print(f"[DONE] xlsx: {xlsx_path}", flush=True)
    return 1 if failures else 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--view-export-csv",
        help="由 export_view_datasets_report 生成的 CSV；省略则从 MySQL 现算",
    )
    parser.add_argument(
        "--eligible-list",
        help="输出符合条件 view 列表（db.table 每行）",
    )
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CONCURRENCY", "10")))
    parser.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    parser.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN") or None)
    parser.add_argument(
        "--platform-instance",
        default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"),
    )
    parser.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    parser.add_argument(
        "--table-prefix",
        default=os.getenv("TABLE_PRE", "").strip(),
        help="仅当未提供 --view-export-csv 时，限制 MySQL 扫描的 view 表名前缀",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "1") == "1",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    setup_logging()
    args = parse_args(argv)
    if args.view_export_csv:
        export_rows = load_view_rows_from_csv(Path(args.view_export_csv))
        logger.info("loaded view export csv: %s rows=%s", args.view_export_csv, len(export_rows))
    else:
        export_rows = build_view_export_rows(
            platform_instance=args.platform_instance,
            env=args.env,
            table_prefix=args.table_prefix,
        )
        logger.info("built view export rows from mysql: %s", len(export_rows))

    eligible = filter_eligible_views(export_rows)
    logger.info(
        "eligible views: %s (upstream_count>0 and view_definition_has_content=是)",
        len(eligible),
    )
    if args.eligible_list:
        out = Path(args.eligible_list)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(row.view_name for row in eligible) + ("\n" if eligible else ""),
            encoding="utf-8",
        )
        logger.info("wrote eligible list: %s", out)

    if not eligible:
        write_jsonl_report(args.jsonl, [])
        write_xlsx_report(args.xlsx, [])
        print("[DONE] no eligible views", flush=True)
        return 0

    return run_batch_update(
        eligible,
        gms_url=args.datahub_gms,
        token=args.token,
        platform_instance=args.platform_instance,
        env=args.env,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        jsonl_path=args.jsonl,
        xlsx_path=args.xlsx,
    )


if __name__ == "__main__":
    sys.exit(main())
