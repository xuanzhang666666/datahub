"""Hive table naming helpers for BLF DataHub MCP."""

from __future__ import annotations

import re
import urllib.parse

DEFAULT_PLATFORM_INSTANCE = "blf-prod-hive"
DEFAULT_ENV = "PROD"
DEFAULT_PUBLIC_BASE_URL = "http://neo4j2.dp.data.bj1.wormpex.com:9002"

_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)+$")


def normalize_hive_table(table: str) -> str:
    """Normalize table input to db.table with BLF default database."""
    normalized = table.strip().lower()
    if not normalized:
        raise ValueError("table cannot be empty")
    if "." not in normalized:
        normalized = f"default.{normalized}"
    if not _TABLE_RE.match(normalized):
        raise ValueError(
            "table must be a Hive table name like 'db.table' or 'table_name'"
        )
    return normalized


def make_hive_dataset_urn(
    table: str,
    platform_instance: str = DEFAULT_PLATFORM_INSTANCE,
    env: str = DEFAULT_ENV,
) -> str:
    """Build a DataHub Hive dataset URN using BLF defaults."""
    normalized = normalize_hive_table(table)
    return (
        "urn:li:dataset:(urn:li:dataPlatform:hive,"
        f"{platform_instance}.{normalized},{env})"
    )


def make_datahub_dataset_url(
    dataset_urn: str,
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL,
) -> str:
    """Build the DataHub UI URL for a dataset URN."""
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{public_base_url.rstrip().rstrip('/')}/dataset/{encoded}"
