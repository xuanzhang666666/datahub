#!/usr/bin/env python3
"""Batch-audit field lineage anomalies for a table list; emit JSONL + summary."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))

from job_info_sync_datahub.field_lineage_datahub_reader import make_hive_dataset_urn
from job_info_sync_datahub.query_upstream_lineage import read_direct_lineage_anomalies

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")
ALIAS_RE = re.compile(r" t\d+", re.I)
MERGED_NAME_RE = re.compile(r"\.[a-z_][a-z0-9_]*\.[a-z_]", re.I)


def fetch_fg_count(table: str) -> tuple[int, list[str]]:
    urn = make_hive_dataset_urn(table)
    url = (
        GMS.rstrip("/")
        + "/openapi/v3/entity/dataset/"
        + urllib.parse.quote(urn, safe="")
        + "?aspects=upstreamLineage"
    )
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    aspect = payload.get("upstreamLineage", {}).get("value", {})
    entries = aspect.get("fineGrainedLineages") or []
    alias_samples: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for upstream in entry.get("upstreams") or []:
            if isinstance(upstream, str) and ALIAS_RE.search(upstream):
                alias_samples.append(upstream[:200])
    return len([entry for entry in entries if isinstance(entry, dict)]), alias_samples


def classify_buckets(field_anomalies: list[str]) -> dict[str, int]:
    buckets = {
        "dataset_not_found": 0,
        "not_direct_upstream": 0,
        "source_field_missing": 0,
        "target_field_missing": 0,
        "invalid_urn": 0,
        "other": 0,
    }
    for line in field_anomalies:
        if "字段源Dataset不存在" in line:
            buckets["dataset_not_found"] += 1
        elif "字段源不属于直接上游" in line:
            buckets["not_direct_upstream"] += 1
        elif "字段源字段不存在" in line:
            buckets["source_field_missing"] += 1
        elif "目标字段不存在" in line:
            buckets["target_field_missing"] += 1
        elif "URN无效" in line or "格式无效" in line:
            buckets["invalid_urn"] += 1
        else:
            buckets["other"] += 1
    return buckets


def dominant_pattern(field_anomalies: list[str], alias_samples: list[str]) -> str:
    if alias_samples:
        return "sql_alias_in_urn"
    if not field_anomalies:
        return "no_field_anomaly"
    joined = "\n".join(field_anomalies)
    if "tmp_" in joined:
        return "tmp_table_source"
    if "(from " in joined or " union " in joined.lower():
        return "cte_or_union_in_source_table"
    if (
        ".data.$." in joined
        or "get_json_object" in joined
        or "extended_info[" in joined
        or ", " in joined and "字段源字段不存在" in joined
    ):
        return "json_or_expression_in_source_field"
    if MERGED_NAME_RE.search(joined):
        return "table_field_merged_in_source_name"
    if "字段源字段不存在" in joined and "字段源Dataset不存在" not in joined:
        return "wrong_source_column_only"
    if "字段源Dataset不存在" in joined:
        return "source_dataset_not_found_other"
    return "mixed_or_other"


def db_prefix(table: str) -> str:
    return table.split(".", 1)[0] if "." in table else "default"


def repair_tier(pattern: str) -> str:
    if pattern in {"sql_alias_in_urn", "table_field_merged_in_source_name"}:
        return "phase1_urn_repair"
    if pattern == "no_field_anomaly":
        return "verify_only"
    if pattern in {"tmp_table_source", "cte_or_union_in_source_table"}:
        return "phase2_reexport_llm"
    if pattern in {
        "json_or_expression_in_source_field",
        "wrong_source_column_only",
        "source_dataset_not_found_other",
        "mixed_or_other",
    }:
        return "phase2_reexport_or_manual"
    return "phase2_reexport_or_manual"


def audit_table(table: str) -> dict[str, Any]:
    urn = make_hive_dataset_urn(table)
    anomalies = read_direct_lineage_anomalies(GMS, TOKEN, urn)
    field_anomalies = anomalies["field_lineage_anomalies"]
    try:
        fg_count, alias_samples = fetch_fg_count(table)
    except Exception as exc:
        fg_count = 0
        alias_samples = []
        field_anomalies = [*field_anomalies, f"fineGrainedLineages_read_failed: {exc}"]
    pattern = dominant_pattern(field_anomalies, alias_samples)
    buckets = classify_buckets(field_anomalies)
    return {
        "table": table,
        "db": db_prefix(table),
        "fg_count": fg_count,
        "field_anomaly_count": len(field_anomalies),
        "table_anomaly_count": len(anomalies["table_lineage_anomalies"]),
        "field_buckets": buckets,
        "dominant_pattern": pattern,
        "repair_tier": repair_tier(pattern),
        "sample_anomalies": field_anomalies[:3],
        "alias_urn_samples": alias_samples[:2],
    }


def load_tables(path: Path) -> list[str]:
    seen: set[str] = set()
    tables: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        table = line.strip()
        if not table or table in seen:
            continue
        seen.add(table)
        tables.append(table)
    return tables


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-jsonl", type=Path, default=None)
    args = parser.parse_args()

    tables = load_tables(args.input)
    records: list[dict[str, Any]] = []
    for index, table in enumerate(tables, start=1):
        record = audit_table(table)
        records.append(record)
        if index % 25 == 0:
            print(f"[PROGRESS] {index}/{len(tables)}", file=sys.stderr, flush=True)

    pattern_counts = Counter(record["dominant_pattern"] for record in records)
    tier_counts = Counter(record["repair_tier"] for record in records)
    db_pattern: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        db_pattern[record["db"]][record["dominant_pattern"]] += 1

    summary = {
        "table_count": len(records),
        "with_field_anomalies": sum(1 for r in records if r["field_anomaly_count"] > 0),
        "no_field_anomaly": pattern_counts.get("no_field_anomaly", 0),
        "pattern_counts": dict(pattern_counts),
        "repair_tier_counts": dict(tier_counts),
        "db_pattern_counts": {
            db: dict(counter) for db, counter in sorted(db_pattern.items())
        },
    }

    if args.output_jsonl:
        args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with args.output_jsonl.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
