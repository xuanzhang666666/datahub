#!/usr/bin/env python3
"""Batch-repair corrupted field-lineage schemaField URNs for Phase-1 tables."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Set

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

from .field_lineage_datahub_reader import (
    extract_upstream_table_names,
    fetch_upstream_table_names,
    make_hive_dataset_urn,
)
from .field_lineage_urn_repair import TableRepairResult, repair_fine_grained_lineage_entry
from .query_upstream_lineage import _fetch_dataset_aspect

PHASE1_PATTERNS = {
    "sql_alias_in_urn",
    "table_field_merged_in_source_name",
}
PHASE1_TIER = "phase1_urn_repair"


def _log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[FIELD_LINEAGE_REPAIR][{now}] {message}", flush=True)


def load_tables_from_text(path: Path) -> List[str]:
    seen: Set[str] = set()
    tables: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        table = line.strip()
        if not table or table in seen:
            continue
        seen.add(table)
        tables.append(table)
    return tables


def load_tables_from_jsonl(
    path: Path,
    *,
    filter_tier: Optional[str],
    filter_patterns: Optional[Set[str]],
) -> List[str]:
    tables: List[str] = []
    seen: Set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if "summary" in record:
            continue
        tier = record.get("repair_tier", "")
        pattern = record.get("dominant_pattern", "")
        if filter_tier and tier != filter_tier:
            continue
        if filter_patterns and pattern not in filter_patterns:
            continue
        table = str(record.get("table", "")).strip()
        if not table or table in seen:
            continue
        seen.add(table)
        tables.append(table)
    return tables


def load_target_tables(
    *,
    input_path: Path,
    filter_tier: Optional[str],
    filter_patterns: Optional[Set[str]],
) -> List[str]:
    if input_path.suffix == ".jsonl":
        return load_tables_from_jsonl(
            input_path,
            filter_tier=filter_tier,
            filter_patterns=filter_patterns,
        )
    tables = load_tables_from_text(input_path)
    if not filter_tier and not filter_patterns:
        return tables
    raise RuntimeError("文本表清单不支持 tier/pattern 过滤，请使用 batch audit 生成的 JSONL")


def repair_one_table(
    target_table: str,
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
    dry_run: bool,
) -> TableRepairResult:
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
    from datahub.metadata.schema_classes import UpstreamLineageClass

    downstream_urn = make_hive_dataset_urn(target_table, platform_instance, env)
    payload = _fetch_dataset_aspect(gms_url, downstream_urn, "upstreamLineage", token=token)
    aspect_payload = payload.get("upstreamLineage", {}).get("value", payload.get("value", {}))
    upstream_tables = set(
        fetch_upstream_table_names(
            gms_url,
            downstream_urn,
            token=token,
            platform_instance=platform_instance,
        )
    )
    if not upstream_tables:
        upstream_tables = set(
            extract_upstream_table_names(payload, platform_instance)
        )

    graph = DataHubGraph(DatahubClientConfig(server=gms_url.rstrip("/"), token=token))
    existing = graph.get_aspect(entity_urn=downstream_urn, aspect_type=UpstreamLineageClass)
    if not existing or not existing.fineGrainedLineages:
        return TableRepairResult(
            target_table=target_table,
            fg_count=0,
            upstream_urn_count=0,
            repaired_upstream_count=0,
            unchanged_upstream_count=0,
            removed_upstream_count=0,
            failed_upstream_count=0,
            written=False,
            dry_run=dry_run,
            failures=("no_fine_grained_lineages",),
        )

    schema_cache: dict[str, Optional[set[str]]] = {}
    repaired_entries = []
    total_changed = total_unchanged = total_removed = total_failed = 0
    failures: List[str] = []
    upstream_urn_count = 0

    for entry in list(existing.fineGrainedLineages):
        upstream_urn_count += len(entry.upstreams or [])
        repaired_entry, changed, unchanged, removed, failed, entry_failures = (
            repair_fine_grained_lineage_entry(
                entry,
                upstream_tables=upstream_tables,
                schema_cache=schema_cache,
                gms_url=gms_url,
                token=token,
                platform_instance=platform_instance,
                env=env,
            )
        )
        total_changed += changed
        total_unchanged += unchanged
        total_removed += removed
        total_failed += failed
        failures.extend(entry_failures)
        repaired_entries.append(repaired_entry)

    result = TableRepairResult(
        target_table=target_table,
        fg_count=len(repaired_entries),
        upstream_urn_count=upstream_urn_count,
        repaired_upstream_count=total_changed,
        unchanged_upstream_count=total_unchanged,
        removed_upstream_count=total_removed,
        failed_upstream_count=total_failed,
        written=False,
        dry_run=dry_run,
        failures=tuple(failures[:20]),
    )

    if dry_run or (total_changed == 0 and total_removed == 0):
        return result

    aspect = UpstreamLineageClass(
        upstreams=list(existing.upstreams) if existing.upstreams else [],
        fineGrainedLineages=repaired_entries,
    )
    emitter = DatahubRestEmitter(gms_url, token=token)
    mcp = MetadataChangeProposalWrapper(entityUrn=downstream_urn, aspect=aspect)
    emitter.emit_mcp(mcp)
    return TableRepairResult(
        target_table=target_table,
        fg_count=len(repaired_entries),
        upstream_urn_count=upstream_urn_count,
        repaired_upstream_count=total_changed,
        unchanged_upstream_count=total_unchanged,
        removed_upstream_count=total_removed,
        failed_upstream_count=total_failed,
        written=True,
        dry_run=dry_run,
        failures=tuple(failures[:20]),
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="表清单 txt 或 batch audit JSONL")
    parser.add_argument("--output", type=Path, default=None, help="修复结果 JSONL")
    parser.add_argument(
        "--filter-tier",
        default=PHASE1_TIER,
        help=f"仅处理指定 repair_tier，默认 {PHASE1_TIER}",
    )
    parser.add_argument(
        "--filter-patterns",
        default=",".join(sorted(PHASE1_PATTERNS)),
        help="逗号分隔 dominant_pattern 过滤；默认 Phase1 两类",
    )
    parser.add_argument("--gms-url", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    parser.add_argument("--gms-token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    parser.add_argument("--platform-instance", default="blf-prod-hive")
    parser.add_argument("--env", default="PROD")
    parser.add_argument("--write", action="store_true", help="写入 DataHub；默认 dry-run")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 张表，0 表示全部")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    patterns = {
        item.strip()
        for item in (args.filter_patterns or "").split(",")
        if item.strip()
    } or None
    filter_tier = args.filter_tier or None
    tables = load_target_tables(
        input_path=args.input,
        filter_tier=filter_tier,
        filter_patterns=patterns,
    )
    if args.limit > 0:
        tables = tables[: args.limit]
    if not tables:
        _log("ERROR: 未加载到待修复表")
        return 2

    _log(
        f"started tables={len(tables)} dry_run={not args.write} "
        f"filter_tier={filter_tier} filter_patterns={sorted(patterns or [])}"
    )

    results: List[TableRepairResult] = []
    written_count = 0
    changed_count = 0
    for index, table in enumerate(tables, start=1):
        try:
            result = repair_one_table(
                table,
                gms_url=args.gms_url,
                token=args.gms_token,
                platform_instance=args.platform_instance,
                env=args.env,
                dry_run=not args.write,
            )
        except Exception as exc:
            result = TableRepairResult(
                target_table=table,
                fg_count=0,
                upstream_urn_count=0,
                repaired_upstream_count=0,
                unchanged_upstream_count=0,
                removed_upstream_count=0,
                failed_upstream_count=1,
                written=False,
                dry_run=not args.write,
                failures=(f"exception:{exc}",),
            )
        results.append(result)
        if result.written:
            written_count += 1
        if result.repaired_upstream_count > 0 or result.removed_upstream_count > 0:
            changed_count += 1
        if index % 20 == 0 or index == len(tables):
            _log(
                f"progress {index}/{len(tables)} "
                f"changed_tables={changed_count} written_tables={written_count}"
            )

    summary = {
        "table_count": len(results),
        "tables_with_repairs": sum(
            1 for item in results if item.repaired_upstream_count > 0 or item.removed_upstream_count > 0
        ),
        "tables_written": written_count,
        "total_repaired_upstream_urns": sum(item.repaired_upstream_count for item in results),
        "total_removed_upstream_urns": sum(item.removed_upstream_count for item in results),
        "total_failed_upstream_urns": sum(item.failed_upstream_count for item in results),
        "dry_run": not args.write,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            for item in results:
                handle.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
            handle.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")

    _log(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
