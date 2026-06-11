"""Policy rules for field-lineage review and import."""

from __future__ import annotations

import re
from typing import Optional, Set, Tuple

PARTITION_FIELDS = {"dt", "hr"}

_TMP_LEAF_PREFIX = "tmp_"
_NOT_VERIFIED_LEAF_PREFIX = "not_verified_"
_SHELL_VAR_IN_NAME_RE = re.compile(r"\$\{")
# Progressive tmp_* stripping (tmp 套 tmp) stops after this many hops.
MAX_EPHEMERAL_SOURCE_FOLD_HOPS = 5

_TABLE_ALIAS_SUFFIX_RE = re.compile(r"\s+(?:as\s+)?t\d+\s*$", re.I)
_FIELD_ALIAS_PREFIX_RE = re.compile(r"^t\d+\.", re.I)


def is_partition_field(field_name: str) -> bool:
    return field_name.strip().lower() in PARTITION_FIELDS


def normalize_table_name(table_name: str) -> str:
    return table_name.strip().lower()


def strip_sql_table_alias(table_name: str) -> str:
    """Remove trailing SQL alias such as ``default.dim_user_hr t2``."""
    return _TABLE_ALIAS_SUFFIX_RE.sub("", table_name.strip()).strip()


def normalize_source_field_name(field_name: str) -> str:
    """Normalize source column name and strip leading SQL alias such as ``t2.user_no``."""
    normalized = field_name.strip().lower().strip("`")
    while _FIELD_ALIAS_PREFIX_RE.match(normalized):
        normalized = _FIELD_ALIAS_PREFIX_RE.sub("", normalized, count=1).strip().strip("`")
    return normalized.strip()


def has_residual_sql_alias_in_source_table(table_name: str) -> bool:
    """Return True when a source table still contains a SQL alias after normalization."""
    stripped = table_name.strip()
    if not stripped:
        return False
    if " " in stripped:
        return True
    return bool(_TABLE_ALIAS_SUFFIX_RE.search(stripped))


def has_unsafe_source_field_pattern(field_name: str) -> bool:
    """Return True for source_field values that must not be written to DataHub URNs."""
    stripped = field_name.strip()
    if not stripped:
        return False
    if "," in stripped:
        return True
    if "[" in stripped or "]" in stripped or "(" in stripped or ")" in stripped:
        return True
    normalized = normalize_source_field_name(stripped)
    if "." in normalized:
        return True
    return bool(_FIELD_ALIAS_PREFIX_RE.match(normalized))


def field_name_in_schema(field_name: str, schema_fields: Optional[Set[str]]) -> bool:
    """Return True when *field_name* matches a non-partition column in *schema_fields*."""
    if schema_fields is None:
        return True
    normalized = field_name.strip().lower().strip("`")
    if not normalized:
        return False
    return normalized in schema_fields


def normalize_source_table_name(table_name: str) -> str:
    normalized = normalize_table_name(strip_sql_table_alias(table_name))
    if normalized and "." not in normalized:
        return f"default.{normalized}"
    return normalized


def is_self_dependency(target_table: str, source_table: str) -> bool:
    target = normalize_table_name(target_table)
    source = normalize_table_name(source_table)
    return bool(target and source and target == source)


def table_leaf(table_name: str) -> str:
    normalized = normalize_source_table_name(table_name)
    if not normalized or "." not in normalized:
        return normalized
    return normalized.rsplit(".", 1)[-1]


def is_ephemeral_source_table(table_name: str) -> bool:
    """Return True when source_table is tmp/not_verified/shell-var, not a durable upstream."""
    stripped = table_name.strip()
    if not stripped:
        return False
    if _SHELL_VAR_IN_NAME_RE.search(stripped):
        return True
    leaf = table_leaf(stripped)
    if not leaf:
        return False
    return leaf.startswith(_TMP_LEAF_PREFIX) or leaf.startswith(_NOT_VERIFIED_LEAF_PREFIX)


def resolve_ephemeral_source_table(
    source_table: str,
    upstream_tables: Set[str],
) -> Tuple[str, str]:
    """Fold ephemeral source_table names toward a direct upstream when possible.

    Strips at most one ``tmp_`` leaf prefix per hop and repeats until the name
    matches an upstream table, is no longer ephemeral, or ``MAX_EPHEMERAL_SOURCE_FOLD_HOPS``
    is reached. This covers nested ``tmp_tmp_*`` chains without SQL parsing.
    """
    from .lineage_write_policy import resolve_not_verified_table_alias

    current = normalize_source_table_name(source_table)
    if not current:
        return "", "empty"

    fold_hops = 0
    last_reason = ""
    for _ in range(MAX_EPHEMERAL_SOURCE_FOLD_HOPS):
        if current in upstream_tables:
            if fold_hops == 0:
                return current, "ok"
            if fold_hops == 1 and last_reason:
                return current, last_reason
            return current, f"folded_tmp_chain:{fold_hops}"

        stripped_nv = resolve_not_verified_table_alias(current)
        if stripped_nv != current:
            current = stripped_nv
            fold_hops += 1
            last_reason = "folded_not_verified"
            continue

        if not is_ephemeral_source_table(current):
            if fold_hops == 0:
                return current, "not_ephemeral"
            return current, f"folded_tmp_chain:{fold_hops}"

        leaf = table_leaf(current)
        if not leaf.startswith(_TMP_LEAF_PREFIX):
            break

        suffix = leaf[len(_TMP_LEAF_PREFIX) :]
        if not suffix:
            break

        db_prefix = current.rsplit(".", 1)[0] if "." in current else "default"
        same_db = f"{db_prefix}.{suffix}"
        leaf_matches = sorted(
            table for table in upstream_tables if table_leaf(table) == suffix
        )

        if same_db in upstream_tables:
            fold_hops += 1
            if fold_hops == 1:
                return same_db, "folded_tmp_same_db"
            return same_db, f"folded_tmp_chain:{fold_hops}"

        if len(leaf_matches) == 1:
            fold_hops += 1
            if fold_hops == 1:
                return leaf_matches[0], "folded_tmp_leaf_match"
            return leaf_matches[0], f"folded_tmp_chain:{fold_hops}"

        if same_db != current:
            current = same_db
            fold_hops += 1
            last_reason = "folded_tmp_strip"
            continue

        break

    if is_ephemeral_source_table(current):
        return current, "ephemeral_unresolved"
    if fold_hops > 0:
        return current, f"folded_tmp_chain:{fold_hops}"
    return current, "not_ephemeral"


def ephemeral_source_fold_note(original: str, resolved: str, reason: str) -> str:
    return f"folded_source:{original}->{resolved} ({reason})"
