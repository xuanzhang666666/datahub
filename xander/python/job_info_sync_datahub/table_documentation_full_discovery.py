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


def table_name_matches_prefix(table_name: str, prefix: str) -> bool:
    """Match ``db.table`` only when the **table** segment starts with *prefix*.

    Examples for ``TABLE_PRE=pdw``:

    - ``ods.pdw_target`` — kept (table name prefix)
    - ``pdw.dim_store`` — excluded (database prefix only)
    """
    p = prefix.strip().lower()
    if not p:
        return True
    t = table_name.strip().lower()
    if "." not in t:
        return False
    _, tbl = t.split(".", 1)
    return tbl.startswith(p)


def filter_candidates_by_table_prefix(
    candidates: list[DatasetDocCandidate],
    prefix: str,
) -> list[DatasetDocCandidate]:
    p = prefix.strip()
    if not p:
        return candidates
    return [item for item in candidates if table_name_matches_prefix(item.table_name, p)]


def filter_table_names_by_prefix(names: Iterable[str], prefix: str) -> list[str]:
    p = prefix.strip()
    if not p:
        return sorted({n.strip().lower() for n in names if n.strip()})
    return sorted(n.strip().lower() for n in names if n.strip() and table_name_matches_prefix(n, p))


def filter_table_names_file(input_path: str, prefix: str, output_path: str) -> int:
    names = Path(input_path).read_text(encoding="utf-8").splitlines()
    filtered = filter_table_names_by_prefix(names, prefix)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(filtered) + ("\n" if filtered else ""), encoding="utf-8")
    return len(filtered)


def _mysql_base_cmd() -> list[str]:
    user = os.getenv("DATAHUB_MYSQL_USER", "root")
    password = os.getenv("DATAHUB_MYSQL_PASSWORD", "datahub")
    database = os.getenv("DATAHUB_MYSQL_DATABASE", "datahub")
    common = [
        f"-u{user}",
        f"-p{password}",
        "-D",
        database,
        "--batch",
        "--raw",
        "--skip-column-names",
    ]

    host = os.getenv("DATAHUB_MYSQL_HOST", "").strip()
    if host:
        port = os.getenv("DATAHUB_MYSQL_PORT", "3306").strip() or "3306"
        mysql_bin = os.getenv("DATAHUB_MYSQL_CLIENT", "mysql").strip() or "mysql"
        return [mysql_bin, f"-h{host}", f"-P{port}", *common]

    container = os.getenv("DATAHUB_MYSQL_CONTAINER", "datahub-mysql-1")
    return ["docker", "exec", container, "mysql", *common]


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
    parser.add_argument(
        "--table-prefix",
        default=os.getenv("TABLE_PRE", "").strip(),
        help="只保留表名（点号后一段）以此前缀开头的 db.table（环境变量 TABLE_PRE）",
    )
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
    before_prefix = len(candidates)
    table_prefix = (args.table_prefix or "").strip()
    if table_prefix:
        candidates = filter_candidates_by_table_prefix(candidates, table_prefix)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(item.table_name for item in candidates) + ("\n" if candidates else ""), encoding="utf-8")

    if args.summary:
        summary = {
            "platform_instance": args.platform_instance,
            "env": args.env,
            "table_prefix": table_prefix or None,
            "table_candidate_count_before_prefix": before_prefix,
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

    prefix_note = f" prefix={table_prefix!r} before={before_prefix}" if table_prefix else ""
    print(
        f"[INFO] structuredProperties datasets={len(structured_rows)} "
        f"views={len(view_urns)} deprecated={len(deprecated_urns)} tables={len(candidates)}{prefix_note}"
    )
    print(f"[INFO] written sorted table list: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
