"""Mark Hive datasets as deprecated and set BLF structured properties.

Designed for Jenkins jobs that pass table names through ``TABLE_NAMES``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from .datahub_writer import make_hive_dataset_urn, patch_structured_properties
from .models import StructuredPropertyValue

URN_ETL_SCRIPT = "urn:li:structuredProperty:blf.data.warehouse.etl_script"
URN_SCHEDULE_URL = "urn:li:structuredProperty:blf.data.schedule.schedule_url"
URN_EXECUTE_SHELL = "urn:li:structuredProperty:blf.data.schedule.execute_shell"
URN_OTHER_REMARK = "urn:li:structuredProperty:blf.data.warehouse.other_remark"

DEFAULT_DEPRECATION_NOTE = "已废弃"
DEFAULT_EMPTY_VALUE = "无"
DEFAULT_ACTOR = "urn:li:corpuser:xuan.zhang"

DeprecationWriter = Callable[[str, str, str, str, Optional[str]], None]
StructuredPropsWriter = Callable[[str, str, list[StructuredPropertyValue], Optional[str]], None]


def parse_table_names(raw: str) -> list[str]:
    """Parse Jenkins multi-line TABLE_NAMES, accepting commas and comments."""
    out: list[str] = []
    for line in raw.replace("\r", "\n").splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        for item in body.split(","):
            name = item.strip().strip("'\"")
            if name:
                out.append(name)
    return out


def read_table_names_from_file(path: str) -> list[str]:
    return parse_table_names(Path(path).read_text(encoding="utf-8"))


def dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def dataset_urn_for_table_name(
    table_name: str,
    *,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    implicit_database: str = "default",
) -> str:
    parts = [p for p in table_name.strip().split(".") if p]
    if len(parts) == 1:
        db, table = implicit_database, parts[0]
    elif len(parts) == 2:
        db, table = parts
    elif len(parts) == 3:
        platform_instance, db, table = parts
    else:
        raise ValueError(f"表名格式非法: {table_name!r}，请使用 表名 / 库.表 / platform_instance.库.表")
    return make_hive_dataset_urn(db, table, platform_instance=platform_instance, env=env)


def build_deprecated_structured_properties(
    *,
    empty_value: str = DEFAULT_EMPTY_VALUE,
    note: str = DEFAULT_DEPRECATION_NOTE,
) -> list[StructuredPropertyValue]:
    return [
        StructuredPropertyValue(property_urn=URN_SCHEDULE_URL, string_value=empty_value),
        StructuredPropertyValue(property_urn=URN_ETL_SCRIPT, string_value=empty_value),
        StructuredPropertyValue(property_urn=URN_OTHER_REMARK, string_value=note),
        StructuredPropertyValue(property_urn=URN_EXECUTE_SHELL, string_value=empty_value),
    ]


def mark_dataset_deprecated(
    gms_url: str,
    dataset_urn: str,
    note: str,
    actor: str,
    token: Optional[str],
) -> None:
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import DeprecationClass

    emitter = DatahubRestEmitter(gms_url, token=token)
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=dataset_urn,
            aspect=DeprecationClass(
                deprecated=True,
                note=note,
                actor=actor,
            ),
        )
    )


def update_tables(
    table_names: list[str],
    *,
    gms_url: str,
    token: Optional[str],
    actor: str,
    platform_instance: str = "blf-prod-hive",
    env: str = "PROD",
    implicit_database: str = "default",
    note: str = DEFAULT_DEPRECATION_NOTE,
    empty_value: str = DEFAULT_EMPTY_VALUE,
    dry_run: bool = False,
    mark_deprecated_fn: DeprecationWriter = mark_dataset_deprecated,
    patch_structured_properties_fn: StructuredPropsWriter = patch_structured_properties,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    props = build_deprecated_structured_properties(empty_value=empty_value, note=note)
    for table_name in dedupe_keep_order(table_names):
        dataset_urn = ""
        try:
            dataset_urn = dataset_urn_for_table_name(
                table_name,
                platform_instance=platform_instance,
                env=env,
                implicit_database=implicit_database,
            )
            if not dry_run:
                mark_deprecated_fn(gms_url, dataset_urn, note, actor, token)
                patch_structured_properties_fn(gms_url, dataset_urn, props, token)
            results.append({"table": table_name, "urn": dataset_urn, "status": "OK", "error": ""})
        except Exception as exc:
            results.append(
                {
                    "table": table_name,
                    "urn": dataset_urn,
                    "status": "FAIL",
                    "error": str(exc),
                }
            )
    return results


def write_jsonl_report(path: str, rows: list[dict[str, str]]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table-name", action="append", default=[], help="目标表名，可重复")
    p.add_argument("--table-list-file", help="表名单文件，一行一个；支持逗号和 # 注释")
    p.add_argument("--datahub-gms", default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN") or None)
    p.add_argument("--actor", default=os.getenv("DATAHUB_ACTOR", DEFAULT_ACTOR))
    p.add_argument("--platform-instance", default=os.getenv("BLF_DATAHUB_PLATFORM_INSTANCE", "blf-prod-hive"))
    p.add_argument("--env", default=os.getenv("DATAHUB_ENV", "PROD"))
    p.add_argument("--implicit-database", default=os.getenv("HIVE_IMPLICIT_DATABASE", "default"))
    p.add_argument("--note", default=os.getenv("DEPRECATION_NOTE", DEFAULT_DEPRECATION_NOTE))
    p.add_argument("--empty-value", default=os.getenv("DEPRECATION_EMPTY_VALUE", DEFAULT_EMPTY_VALUE))
    p.add_argument("--report", default=os.getenv("DEPRECATE_TABLE_REPORT", ""))
    p.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "0") == "1")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    table_names: list[str] = []
    if args.table_list_file:
        table_names.extend(read_table_names_from_file(args.table_list_file))
    table_names.extend(args.table_name)
    table_names.extend(parse_table_names(os.getenv("TABLE_NAMES", "")))
    table_names = dedupe_keep_order([n.strip() for n in table_names if n.strip()])

    if not table_names:
        print("ERROR: 请通过 TABLE_NAMES、--table-name 或 --table-list-file 提供表名", file=sys.stderr)
        return 2

    rows = update_tables(
        table_names,
        gms_url=args.datahub_gms,
        token=args.token,
        actor=args.actor,
        platform_instance=args.platform_instance,
        env=args.env,
        implicit_database=args.implicit_database,
        note=args.note,
        empty_value=args.empty_value,
        dry_run=args.dry_run,
    )

    for row in rows:
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.report:
        write_jsonl_report(args.report, rows)
        print(f"[INFO] report written: {args.report}", flush=True)

    return 1 if any(row["status"] != "OK" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
