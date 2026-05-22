#!/usr/bin/env python3
"""Discover Hive table datasets that have structured ETL properties."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "job_info_sync_datahub"

from .field_lineage_datahub_reader import strip_markdown_code_fence
from .structured_properties import URN_ETL_SCRIPT, URN_EXECUTE_SHELL

_HIVE_DATASET_URN_RE = re.compile(
    r"^urn:li:dataset:\(urn:li:dataPlatform:hive,(?P<name>[^,]+),(?P<env>[^)]+)\)$"
)


@dataclass(frozen=True)
class DatasetDocCandidate:
    urn: str
    table_name: str
    source_properties: tuple[str, ...]


def parse_hive_dataset_urn(
    urn: str,
    *,
    platform_instance: str,
    env: str,
) -> Optional[str]:
    match = _HIVE_DATASET_URN_RE.match(urn.strip())
    if not match:
        return None
    if match.group("env") != env:
        return None
    name = match.group("name")
    prefix = f"{platform_instance}."
    if not name.startswith(prefix):
        return None
    table_name = name[len(prefix) :].strip().lower()
    if table_name.count(".") != 1:
        return None
    return table_name


def _first_string_value(prop: dict) -> str:
    values = prop.get("values")
    if not isinstance(values, list):
        return ""
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("string"), str):
            return value["string"]
        if isinstance(value, str):
            return value
    return ""


def is_meaningful_doc_source_text(value: str) -> bool:
    text = strip_markdown_code_fence(value).strip()
    return bool(text) and text not in {"无"}


def structured_doc_sources(metadata: str) -> tuple[str, ...]:
    try:
        payload = json.loads(metadata)
    except json.JSONDecodeError:
        return ()

    properties = payload.get("properties")
    if not isinstance(properties, list):
        return ()

    sources: list[str] = []
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        urn = prop.get("propertyUrn")
        if urn not in (URN_ETL_SCRIPT, URN_EXECUTE_SHELL):
            continue
        if is_meaningful_doc_source_text(_first_string_value(prop)):
            sources.append("Etl Script" if urn == URN_ETL_SCRIPT else "Execute Shell")
    return tuple(sources)


def discover_candidates_from_rows(
    structured_rows: Iterable[tuple[str, str]],
    view_urns: set[str],
    deprecated_urns: set[str],
    *,
    platform_instance: str,
    env: str,
) -> list[DatasetDocCandidate]:
    candidates: list[DatasetDocCandidate] = []
    seen: set[str] = set()
    for urn, metadata in structured_rows:
        if urn in view_urns or urn in deprecated_urns:
            continue
        table_name = parse_hive_dataset_urn(urn, platform_instance=platform_instance, env=env)
        if not table_name or table_name in seen:
            continue
        sources = structured_doc_sources(metadata)
        if not sources:
            continue
        seen.add(table_name)
        candidates.append(DatasetDocCandidate(urn=urn, table_name=table_name, source_properties=sources))
    return sorted(candidates, key=lambda item: item.table_name)


def _mysql_base_cmd() -> list[str]:
    container = os.getenv("DATAHUB_MYSQL_CONTAINER", "datahub-mysql-1")
    user = os.getenv("DATAHUB_MYSQL_USER", "root")
    password = os.getenv("DATAHUB_MYSQL_PASSWORD", "datahub")
    database = os.getenv("DATAHUB_MYSQL_DATABASE", "datahub")
    return [
        "docker",
        "exec",
        container,
        "mysql",
        f"-u{user}",
        f"-p{password}",
        "-D",
        database,
        "--batch",
        "--raw",
        "--skip-column-names",
    ]


def _run_mysql_query(sql: str) -> str:
    cmd = _mysql_base_cmd() + ["-e", sql]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or "no output"
        raise RuntimeError(f"MySQL 查询失败 exit={proc.returncode}: {detail[:2000]}")
    return proc.stdout.decode("utf-8", errors="replace")


def _parse_tab_rows(output: str, expected_columns: int) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for raw in output.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t", expected_columns - 1)
        if len(parts) == expected_columns:
            rows.append(tuple(parts))
    return rows


def load_structured_rows_from_mysql() -> list[tuple[str, str]]:
    sql = (
        "select urn, metadata "
        "from metadata_aspect_v2 "
        "where aspect='structuredProperties' "
        "and version=0 "
        "and urn like 'urn:li:dataset:(urn:li:dataPlatform:hive,%' "
        "order by urn"
    )
    return [(urn, metadata) for urn, metadata in _parse_tab_rows(_run_mysql_query(sql), 2)]


def load_view_urns_from_mysql() -> set[str]:
    sql = (
        "select urn "
        "from metadata_aspect_v2 "
        "where aspect='viewProperties' "
        "and version=0 "
        "and urn like 'urn:li:dataset:(urn:li:dataPlatform:hive,%'"
    )
    return {row[0] for row in _parse_tab_rows(_run_mysql_query(sql), 1)}


def load_deprecated_urns_from_mysql() -> set[str]:
    sql = (
        "select urn "
        "from metadata_aspect_v2 "
        "where aspect='deprecation' "
        "and version=0 "
        "and metadata like '%\"deprecated\":true%' "
        "and urn like 'urn:li:dataset:(urn:li:dataPlatform:hive,%'"
    )
    return {row[0] for row in _parse_tab_rows(_run_mysql_query(sql), 1)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="输出排序后的 db.table 列表")
    parser.add_argument("--summary", help="输出发现结果 JSON")
    parser.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"))
    parser.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    structured_rows = load_structured_rows_from_mysql()
    view_urns = load_view_urns_from_mysql()
    deprecated_urns = load_deprecated_urns_from_mysql()
    candidates = discover_candidates_from_rows(
        structured_rows,
        view_urns,
        deprecated_urns,
        platform_instance=args.platform_instance,
        env=args.env,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(item.table_name for item in candidates) + ("\n" if candidates else ""), encoding="utf-8")

    if args.summary:
        summary = {
            "platform_instance": args.platform_instance,
            "env": args.env,
            "structured_dataset_count": len(structured_rows),
            "view_dataset_count": len(view_urns),
            "deprecated_dataset_count": len(deprecated_urns),
            "table_candidate_count": len(candidates),
            "tables": [
                {
                    "table": item.table_name,
                    "urn": item.urn,
                    "source_properties": list(item.source_properties),
                }
                for item in candidates
            ],
        }
        summary_path = Path(args.summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[INFO] structuredProperties datasets={len(structured_rows)} "
        f"views={len(view_urns)} deprecated={len(deprecated_urns)} tables={len(candidates)}"
    )
    print(f"[INFO] written sorted table list: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
