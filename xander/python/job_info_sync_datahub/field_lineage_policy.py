"""Policy rules for field-lineage review and import."""

from __future__ import annotations

PARTITION_FIELDS = {"dt"}


def is_partition_field(field_name: str) -> bool:
    return field_name.strip().lower() in PARTITION_FIELDS

