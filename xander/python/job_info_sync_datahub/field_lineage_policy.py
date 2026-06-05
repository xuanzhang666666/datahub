"""Policy rules for field-lineage review and import."""

from __future__ import annotations

PARTITION_FIELDS = {"dt"}


def is_partition_field(field_name: str) -> bool:
    return field_name.strip().lower() in PARTITION_FIELDS


def normalize_table_name(table_name: str) -> str:
    return table_name.strip().lower()


def is_self_dependency(target_table: str, source_table: str) -> bool:
    target = normalize_table_name(target_table)
    source = normalize_table_name(source_table)
    return bool(target and source and target == source)
