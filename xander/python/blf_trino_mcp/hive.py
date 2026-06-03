"""Hive table and SQL helpers for BLF Trino MCP."""

from __future__ import annotations

import re

DEFAULT_CATALOG = "hive"
DEFAULT_SCHEMA = "default"
MAX_QUERY_ROWS = 100000

_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$")
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_READ_ONLY_SQL_RE = re.compile(
    r"^\s*(select|with|show|describe|desc|explain)\b",
    re.IGNORECASE | re.DOTALL,
)
_LIMIT_RE = re.compile(r"\blimit\s+\d+\s*;?\s*$", re.IGNORECASE | re.DOTALL)
_SEMICOLON_RE = re.compile(r";\s*\S", re.DOTALL)


def normalize_hive_table(table: str) -> str:
    """Normalize table input to db.table with BLF default database."""
    normalized = table.strip().lower()
    if not normalized:
        raise ValueError("table cannot be empty")
    if "." not in normalized:
        normalized = f"{DEFAULT_SCHEMA}.{normalized}"
    if not _TABLE_RE.match(normalized):
        raise ValueError(
            "table must be a Hive table name like 'db.table' or 'table_name'"
        )
    return normalized


def split_hive_table(table: str) -> tuple[str, str]:
    """Return database and table name."""
    normalized = normalize_hive_table(table)
    database, table_name = normalized.split(".", 1)
    return database, table_name


def quote_identifier(identifier: str) -> str:
    """Quote a Hive identifier for Trino SQL."""
    if not _IDENT_RE.match(identifier):
        raise ValueError(f"invalid Hive identifier: {identifier}")
    return f'"{identifier}"'


def qualified_table_sql(table: str, *, catalog: str = DEFAULT_CATALOG) -> str:
    """Return catalog.db.table SQL path with quoted table identifier."""
    database, table_name = split_hive_table(table)
    return f"{catalog}.{database}.{quote_identifier(table_name)}"


def partitions_table_sql(table: str, *, catalog: str = DEFAULT_CATALOG) -> str:
    """Return the Trino Hive $partitions virtual table path."""
    database, table_name = split_hive_table(table)
    return f'{catalog}.{database}."{table_name}$partitions"'


def ensure_read_only_sql(sql: str) -> str:
    """Validate that SQL is a single read-only statement."""
    cleaned = (sql or "").strip()
    if not cleaned:
        raise ValueError("sql cannot be empty")
    if _SEMICOLON_RE.search(cleaned):
        raise ValueError("only one SQL statement is allowed")
    if not _READ_ONLY_SQL_RE.match(cleaned):
        raise ValueError("only read-only SQL is allowed: SELECT/WITH/SHOW/DESCRIBE/EXPLAIN")
    return cleaned.rstrip(";").strip()


def apply_limit(sql: str, row_limit: int) -> str:
    """Append LIMIT to SELECT/WITH SQL when no final LIMIT exists."""
    cleaned = ensure_read_only_sql(sql)
    limit = min(max(int(row_limit), 1), MAX_QUERY_ROWS)
    if not re.match(r"^\s*(select|with)\b", cleaned, re.IGNORECASE | re.DOTALL):
        return cleaned
    if _LIMIT_RE.search(cleaned):
        return cleaned
    return f"{cleaned}\nLIMIT {limit}"
