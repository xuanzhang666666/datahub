"""Repair corrupted fine-grained lineage schemaField URNs in DataHub."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .field_lineage_datahub_reader import (
    dataset_schema_exists,
    make_hive_dataset_urn,
)
from .field_lineage_policy import (
    normalize_source_field_name,
    normalize_source_table_name,
    strip_sql_table_alias,
)
from .query_upstream_lineage import _parse_schema_field_urn, _read_schema_fields, urn_to_table_name

try:
    from datahub.emitter import mce_builder as builder

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


@dataclass(frozen=True)
class RepairedSourceRef:
    source_table: str
    source_field: str
    source_dataset_urn: str
    schema_field_urn: str
    changed: bool
    reason: str = ""


@dataclass(frozen=True)
class TableRepairResult:
    target_table: str
    fg_count: int
    upstream_urn_count: int
    repaired_upstream_count: int
    unchanged_upstream_count: int
    removed_upstream_count: int
    failed_upstream_count: int
    written: bool
    dry_run: bool
    failures: Tuple[str, ...] = ()


_TABLE_PREFIX_DBS = frozenset(
    {"pdw", "ods", "dim", "dw", "mid", "dm", "app", "dwd", "dwa", "pdim"}
)


def fix_malformed_hive_table_name(table_name: str) -> str:
    """Fix names like ``pdw.order_store_91`` -> ``default.pdw_order_store_91``."""
    table = strip_sql_table_alias(table_name).strip().lower()
    parts = [part for part in table.split(".") if part]
    if len(parts) == 2 and parts[0] in _TABLE_PREFIX_DBS:
        return f"default.{parts[0]}_{parts[1]}"
    return normalize_source_table_name(table)


def split_merged_source_table(table_name: str, field_name: str) -> Tuple[str, str]:
    """Split ``db.table.field`` mistakenly stored in the table name."""
    table = fix_malformed_hive_table_name(table_name)
    field = normalize_source_field_name(field_name)
    parts = [part for part in table.split(".") if part]
    if len(parts) >= 3:
        merged_table = ".".join(parts[:-1])
        merged_field = parts[-1]
        if not field:
            field = merged_field
        return fix_malformed_hive_table_name(merged_table), field
    return table, field


def resolve_table_against_upstreams(
    source_table: str,
    upstream_tables: Set[str],
) -> Optional[str]:
    if source_table in upstream_tables:
        return source_table
    leaf = source_table.rsplit(".", 1)[-1]
    db_prefix = source_table.rsplit(".", 1)[0] if "." in source_table else ""
    leaf_matches = sorted(
        table for table in upstream_tables if table.rsplit(".", 1)[-1] == leaf
    )
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    if ".ods_" in source_table:
        pdw_candidate = source_table.replace(".ods_", ".pdw_", 1)
        if pdw_candidate in upstream_tables:
            return pdw_candidate
    if ".pdw_" in source_table:
        ods_candidate = source_table.replace(".pdw_", ".ods_", 1)
        if ods_candidate in upstream_tables:
            return ods_candidate
    if len(leaf_matches) > 1 and db_prefix:
        same_db_matches = sorted(
            table for table in leaf_matches if table.startswith(f"{db_prefix}.")
        )
        if len(same_db_matches) == 1:
            return same_db_matches[0]
    suffix_matches = sorted(
        table
        for table in upstream_tables
        if source_table.endswith(f".{table}") or table.endswith(source_table)
    )
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return None


def repair_source_ref(
    *,
    source_table: str,
    source_field: str,
    upstream_tables: Set[str],
    schema_cache: Dict[str, Optional[Set[str]]],
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
) -> Optional[RepairedSourceRef]:
    candidate_table, candidate_field = split_merged_source_table(source_table, source_field)
    if not candidate_table or not candidate_field:
        return None

    resolved_table = resolve_table_against_upstreams(candidate_table, upstream_tables)
    if resolved_table is None:
        return None
    if not dataset_schema_exists(
        gms_url,
        resolved_table,
        token=token,
        platform_instance=platform_instance,
        env=env,
    ):
        return None

    source_dataset_urn = make_hive_dataset_urn(resolved_table, platform_instance, env)
    schema_fields = _read_schema_fields(gms_url, token, source_dataset_urn, schema_cache)
    if not schema_fields or candidate_field not in schema_fields:
        return None

    if not _SDK_AVAILABLE:
        raise RuntimeError("需要安装 acryl-datahub 才能生成 schemaField URN")

    schema_field_urn = builder.make_schema_field_urn(source_dataset_urn, candidate_field)
    original_table, original_field = split_merged_source_table(source_table, source_field)
    changed = (
        resolved_table != normalize_source_table_name(strip_sql_table_alias(source_table))
        or candidate_field != normalize_source_field_name(source_field)
        or original_table != resolved_table
    )
    reason = "ok"
    if strip_sql_table_alias(source_table) != candidate_table:
        reason = "stripped_sql_alias"
    elif len([part for part in normalize_source_table_name(source_table).split(".") if part]) >= 3:
        reason = "split_merged_table_field"
    elif resolved_table != candidate_table:
        reason = "resolved_upstream_table"

    return RepairedSourceRef(
        source_table=resolved_table,
        source_field=candidate_field,
        source_dataset_urn=source_dataset_urn,
        schema_field_urn=schema_field_urn,
        changed=changed,
        reason=reason,
    )


def repair_schema_field_urn(
    upstream_urn: str,
    *,
    upstream_tables: Set[str],
    schema_cache: Dict[str, Optional[Set[str]]],
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
) -> Tuple[Optional[str], str]:
    parsed = _parse_schema_field_urn(upstream_urn)
    if parsed is None:
        return None, "invalid_schema_field_urn"
    dataset_urn, field_name = parsed
    source_table = urn_to_table_name(dataset_urn, platform_instance)
    repaired = repair_source_ref(
        source_table=source_table,
        source_field=field_name,
        upstream_tables=upstream_tables,
        schema_cache=schema_cache,
        gms_url=gms_url,
        token=token,
        platform_instance=platform_instance,
        env=env,
    )
    if repaired is None:
        return None, f"unresolved:{source_table}.{field_name}"
    if repaired.schema_field_urn == upstream_urn:
        return upstream_urn, "unchanged"
    return repaired.schema_field_urn, repaired.reason


def repair_fine_grained_lineage_entry(
    entry: object,
    *,
    upstream_tables: Set[str],
    schema_cache: Dict[str, Optional[Set[str]]],
    gms_url: str,
    token: Optional[str],
    platform_instance: str,
    env: str,
    drop_unresolved: bool = True,
) -> Tuple[object, int, int, int, int, List[str]]:
    upstream_values = list(getattr(entry, "upstreams", None) or [])
    repaired_values: List[str] = []
    seen: Set[str] = set()
    changed = unchanged = removed = failed = 0
    failures: List[str] = []

    for upstream_urn in upstream_values:
        if not isinstance(upstream_urn, str):
            failed += 1
            failures.append(f"non_string_upstream:{upstream_urn!r}")
            continue
        new_urn, status = repair_schema_field_urn(
            upstream_urn,
            upstream_tables=upstream_tables,
            schema_cache=schema_cache,
            gms_url=gms_url,
            token=token,
            platform_instance=platform_instance,
            env=env,
        )
        if new_urn is None:
            failed += 1
            failures.append(f"{upstream_urn} -> {status}")
            if not drop_unresolved:
                repaired_values.append(upstream_urn)
                if upstream_urn not in seen:
                    seen.add(upstream_urn)
            else:
                removed += 1
            continue
        if new_urn not in seen:
            seen.add(new_urn)
            repaired_values.append(new_urn)
        if status == "unchanged":
            unchanged += 1
        else:
            changed += 1

    entry.upstreams = repaired_values
    return entry, changed, unchanged, removed, failed, failures
