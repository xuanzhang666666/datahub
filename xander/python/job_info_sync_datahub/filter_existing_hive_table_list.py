#!/usr/bin/env python3
"""Filter Hive table list to only datasets missing from DataHub."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .hive_single_table_ingest import dataset_entity_exists
from .models import TableRef


def parse_table_line(line: str, implicit_database: Optional[str] = None) -> Optional[TableRef]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "." not in s:
        db = (implicit_database or "default").strip() or "default"
        return TableRef(db=db, table=s)
    db, table = s.split(".", 1)
    db = db.strip()
    table = table.strip()
    if not db or not table:
        return None
    return TableRef(db=db, table=table)


def load_table_refs(
    path: Path,
    *,
    implicit_database: Optional[str] = None,
    database: Optional[str] = None,
) -> List[TableRef]:
    refs: List[TableRef] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        ref = parse_table_line(line, implicit_database)
        if ref is None:
            continue
        if database and ref.db != database:
            continue
        key = ref.full_name.lower()
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


def split_existing_and_missing(
    refs: Sequence[TableRef],
    *,
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
) -> tuple[List[TableRef], List[TableRef]]:
    existing: List[TableRef] = []
    missing: List[TableRef] = []
    for ref in refs:
        if dataset_entity_exists(
            gms_url,
            ref,
            platform_instance=platform_instance,
            env=env,
            token=token,
        ):
            existing.append(ref)
        else:
            missing.append(ref)
    return existing, missing


def write_table_list(path: Path, refs: Sequence[TableRef]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{ref.full_name}\n" for ref in refs), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table-list", required=True, type=Path)
    p.add_argument("--missing-out", required=True, type=Path)
    p.add_argument("--existing-out", required=True, type=Path)
    p.add_argument("--database", default=None)
    p.add_argument("--implicit-database", default=os.getenv("HIVE_INGEST_IMPLICIT_DATABASE"))
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument(
        "--platform-instance",
        default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"),
    )
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.table_list.is_file():
        print(f"ERROR: 表名单文件不存在: {args.table_list}", file=sys.stderr)
        return 2
    refs = load_table_refs(
        args.table_list,
        implicit_database=args.implicit_database,
        database=args.database,
    )
    existing, missing = split_existing_and_missing(
        refs,
        gms_url=args.datahub_gms,
        token=args.token,
        platform_instance=args.platform_instance,
        env=args.env,
    )
    write_table_list(args.existing_out, existing)
    write_table_list(args.missing_out, missing)
    print(
        "[filter_existing_hive_table_list] "
        f"input={len(refs)} existing_skipped={len(existing)} missing_to_ingest={len(missing)} "
        f"missing_out={args.missing_out} existing_out={args.existing_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
